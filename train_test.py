import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
from tqdm import tqdm
import json
import os
import math
import torchvision
from torchvision import transforms
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.tensorboard import SummaryWriter

from evaluator import Evaluator_PC, Evaluator_PC_Top3
from model import *
from utils import *
from dataset_utils import *
from sup_contrast.losses import SupConLoss, SupConLossHierar


def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12354'
    dist.init_process_group("gloo", rank=rank, world_size=world_size)


def train_local(gpu, args, train_subset, test_subset, train_dataset, test_dataset):
    """
    This function trains and evaluates the local prediction module on predicate classification tasks.
    :param gpu: current gpu index
    :param args: input arguments in config.yaml
    :param train_subset: training dataset
    :param test_subset: testing dataset
    """
    rank = gpu
    world_size = torch.cuda.device_count()
    setup(rank, world_size)
    print('rank', rank, 'torch.distributed.is_initialized', torch.distributed.is_initialized())

    writer = None
    if rank == 0:
        writer = SummaryWriter('runs/train_sg')

    train_sampler = torch.utils.data.distributed.DistributedSampler(train_subset, num_replicas=world_size, rank=rank)
    train_loader = torch.utils.data.DataLoader(train_subset, batch_size=args['training']['batch_size'], shuffle=False, collate_fn=collate_fn, num_workers=0, drop_last=True, sampler=train_sampler)
    test_sampler = torch.utils.data.distributed.DistributedSampler(test_subset, num_replicas=world_size, rank=rank)
    test_loader = torch.utils.data.DataLoader(test_subset, batch_size=args['training']['batch_size'], shuffle=False, collate_fn=collate_fn, num_workers=0, drop_last=True, sampler=test_sampler)
    print("Finished loading the datasets...")

    start = []
    if not args['training']['continue_train']:
        record = []
        test_record = []
        with open(args['training']['result_path'] + 'train_results_' + str(rank) + '.json', 'w') as f:  # clear history logs
            json.dump(start, f)
        with open(args['training']['result_path'] + 'test_results_' + str(rank) + '.json', 'w') as f:  # clear history logs
            json.dump(start, f)
    else:
        with open(args['training']['result_path'] + 'train_results_' + str(rank) + '.json', 'r') as f:
            record = json.load(f)
        with open(args['training']['result_path'] + 'test_results_' + str(rank) + '.json', 'r') as f:
            test_record = json.load(f)

    if args['models']['hierarchical_pred']:
        local_predictor = DDP(HierMotif(args=args, input_dim=args['models']['hidden_dim'], feature_size=args['models']['feature_size'],
                                        num_classes=args['models']['num_classes'], num_super_classes=args['models']['num_super_classes'],
                                        num_geometric=args['models']['num_geometric'], num_possessive=args['models']['num_possessive'],
                                        num_semantic=args['models']['num_semantic'])).to(rank)
    else:
        local_predictor = DDP(FlatMotif(args=args, input_dim=args['models']['hidden_dim'], output_dim=args['models']['num_relations'], feature_size=args['models']['feature_size'],
                                        num_classes=args['models']['num_classes'])).to(rank)

    if args['models']['detr_or_faster_rcnn'] == 'detr':
        detr = DDP(build_detr101(args)).to(rank)
        detr.eval()
    else:
        print('Unknown model.')

    map_location = {'cuda:%d' % rank: 'cuda:%d' % 0}
    if args['training']['continue_train']:
        if args['models']['hierarchical_pred']:
            local_predictor.load_state_dict(torch.load(args['training']['checkpoint_path'] + 'HierMotif_CS' + str(args['training']['start_epoch'] - 1) + '_0' + '.pth', map_location=map_location))
        else:
            local_predictor.load_state_dict(torch.load(args['training']['checkpoint_path'] + 'FlatMotif_Semi' + str(args['training']['start_epoch'] - 1) + '_0' + '.pth', map_location=map_location))

    optimizer = optim.SGD([{'params': local_predictor.parameters(), 'initial_lr': args['training']['learning_rate']}],
                          lr=args['training']['learning_rate'], momentum=0.9, weight_decay=args['training']['weight_decay'])
    local_predictor.train()

    original_lr = optimizer.param_groups[0]["lr"]
    relation_count = get_num_each_class_reordered(args)
    class_weight = 1 - relation_count / torch.sum(relation_count)

    if args['models']['hierarchical_pred']:
        criterion_relationship_1 = torch.nn.NLLLoss(weight=class_weight[:args['models']['num_geometric']].to(rank))  # log softmax already applied
        criterion_relationship_2 = torch.nn.NLLLoss(weight=class_weight[args['models']['num_geometric']:args['models']['num_geometric']+args['models']['num_possessive']].to(rank))
        criterion_relationship_3 = torch.nn.NLLLoss(weight=class_weight[args['models']['num_geometric']+args['models']['num_possessive']:].to(rank))
        criterion_super_relationship = torch.nn.NLLLoss()
        criterion_relationship = [criterion_relationship_1, criterion_relationship_2, criterion_relationship_3, criterion_super_relationship]
    else:
        criterion_relationship = torch.nn.CrossEntropyLoss(weight=class_weight.to(rank))
    criterion_contrast = SupConLossHierar()
    criterion_connectivity = torch.nn.BCEWithLogitsLoss()
    criterion_pseudo_consistency = torch.nn.MSELoss()

    running_losses, running_loss_connectivity, running_loss_relationship, running_loss_contrast, running_loss_pseudo_consistency, running_loss_commonsense, \
        connectivity_recall, connectivity_precision, num_connected, num_not_connected, num_connected_pred = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs, wmap_rel, wmap_phrase = None, None, None, None, None, None, None, None

    Recall = Evaluator_PC(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
    if args['dataset']['dataset'] == 'vg':
        Recall_top3 = Evaluator_PC_Top3(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])

    # commonsense_yes_triplets = torch.load('training_triplets.pt')
    dict_relation_names = relation_by_super_class_int2str()
    dict_object_names = object_class_int2str()
    commonsense_yes_triplets = torch.load('commonsense_yes_triplets.pt')
    commonsense_no_triplets = torch.load('commonsense_no_triplets.pt')
    print('commonsense_yes_triplets', [dict_object_names[triplet[0]] + ' ' + dict_relation_names[triplet[1]] + ' ' + dict_object_names[triplet[2]] for triplet in commonsense_yes_triplets])
    print('commonsense_no_triplets', [dict_object_names[triplet[0]] + ' ' + dict_relation_names[triplet[1]] + ' ' + dict_object_names[triplet[2]] for triplet in commonsense_no_triplets])

    lr_decay = 1
    for epoch in range(args['training']['start_epoch'], args['training']['num_epoch']):
        print('Start Training... EPOCH %d / %d\n' % (epoch, args['training']['num_epoch']))
        if epoch == args['training']['scheduler_param1'] or epoch == args['training']['scheduler_param2']:  # lr scheduler
            lr_decay *= 0.1
        lambda_pseudo = 0 #sigmoid_rampup(epoch, args['training']['num_epoch']) if args['training']['run_mode'] == 'train_semi' else 1
        print('lambda_pseudo', lambda_pseudo)

        for batch_count, data in enumerate(tqdm(train_loader), 0):
            """
            PREPARE INPUT DATA
            """
            try:
                if args['training']['run_mode'] == 'train_semi':
                    images, images_aug, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, _, pseudo_label_mask = data
                else:
                    images, images_aug, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, _ = data
            except:
                continue
            batch_size = len(images)

            # with torch.no_grad():
            #     images = torch.stack(images).to(rank)
            #     image_feature, pos_embed = detr.module.backbone(nested_tensor_from_tensor_list(images))
            #     src, mask = image_feature[-1].decompose()
            #     src = detr.module.input_proj(src).flatten(2).permute(2, 0, 1)
            #     pos_embed = pos_embed[-1].flatten(2).permute(2, 0, 1)
            #     image_feature = detr.module.transformer.encoder(src, src_key_padding_mask=mask.flatten(1), pos=pos_embed)
            #     image_feature = image_feature.permute(1, 2, 0)
            #     image_feature = image_feature.view(-1, args['models']['num_img_feature'], args['models']['feature_size'], args['models']['feature_size'])
            #
            #     images_aug = torch.stack(images_aug).to(rank)
            #     image_feature_aug, pos_embed = detr.module.backbone(nested_tensor_from_tensor_list(images_aug))
            #     src, mask = image_feature_aug[-1].decompose()
            #     src = detr.module.input_proj(src).flatten(2).permute(2, 0, 1)
            #     pos_embed = pos_embed[-1].flatten(2).permute(2, 0, 1)
            #     image_feature_aug = detr.module.transformer.encoder(src, src_key_padding_mask=mask.flatten(1), pos=pos_embed)
            #     image_feature_aug = image_feature_aug.permute(1, 2, 0)
            #     image_feature_aug = image_feature_aug.view(-1, args['models']['num_img_feature'], args['models']['feature_size'], args['models']['feature_size'])
            #     del images, images_aug
            #
            # categories = [category.to(rank) for category in categories]  # [batch_size][curr_num_obj, 1]
            # if super_categories[0] is not None:
            #     super_categories = [[sc.to(rank) for sc in super_category] for super_category in super_categories]  # [batch_size][curr_num_obj, [1 or more]]
            # image_depth = torch.stack([depth.to(rank) for depth in image_depth])
            # bbox = [box.to(rank) for box in bbox]  # [batch_size][curr_num_obj, 4]
            # optimizer.param_groups[0]["lr"] = original_lr
            #
            # masks = []
            # for i in range(len(bbox)):
            #     mask = torch.zeros(bbox[i].shape[0], args['models']['feature_size'], args['models']['feature_size'], dtype=torch.bool).to(rank)
            #     for j, box in enumerate(bbox[i]):
            #         mask[j, int(bbox[i][j][2]):int(bbox[i][j][3]), int(bbox[i][j][0]):int(bbox[i][j][1])] = 1
            #     masks.append(mask)
            #
            # """
            # PREPARE TARGETS
            # """
            # relations_target = []
            # direction_target = []
            # pseudo_label_mask_temp = []
            # num_graph_iter = torch.as_tensor([len(mask) for mask in masks]) - 1
            # for graph_iter in range(max(num_graph_iter)):
            #     keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)
            #     relations_target.append(torch.vstack([relationships[i][graph_iter] for i in keep_in_batch]).T.to(rank))  # integer labels
            #     direction_target.append(torch.vstack([subj_or_obj[i][graph_iter] for i in keep_in_batch]).T.to(rank))
            #     if args['training']['run_mode'] == 'train_semi':
            #         pseudo_label_mask_temp.append(torch.vstack([pseudo_label_mask[i][graph_iter] for i in keep_in_batch]).T.to(rank))
            # pseudo_label_mask = pseudo_label_mask_temp
            #
            # """
            # FORWARD PASS
            # """
            # hidden_cat_accumulated = [[] for _ in range(batch_size)]
            # hidden_cat_labels_accumulated = [[] for _ in range(batch_size)]
            # connected_indices_accumulated = []
            # losses, loss_connectivity, loss_relationship, loss_contrast, loss_pseudo_consistency, loss_commonsense = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
            #
            # num_graph_iter = torch.as_tensor([len(mask) for mask in masks])
            # for graph_iter in range(max(num_graph_iter)):
            #     keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1).to(rank)
            #     optimizer.param_groups[0]["lr"] = original_lr * lr_decay * math.sqrt(len(keep_in_batch) / len(num_graph_iter))  # dynamic batch size needs dynamic learning rate
            #
            #     curr_graph_masks = torch.stack([torch.unsqueeze(masks[i][graph_iter], dim=0) for i in keep_in_batch])
            #     h_graph = torch.cat((image_feature[keep_in_batch] * curr_graph_masks, image_depth[keep_in_batch] * curr_graph_masks), dim=1)  # (bs, 256, 64, 64), (bs, 1, 64, 64)
            #     h_graph_aug = torch.cat((image_feature_aug[keep_in_batch] * curr_graph_masks, image_depth[keep_in_batch] * curr_graph_masks), dim=1)
            #     cat_graph = torch.tensor([torch.unsqueeze(categories[i][graph_iter], dim=0) for i in keep_in_batch]).to(rank)
            #     scat_graph = [super_categories[i][graph_iter] for i in keep_in_batch] if super_categories[0] is not None else None
            #     bbox_graph = torch.stack([bbox[i][graph_iter] for i in keep_in_batch]).to(rank)
            #
            #     for edge_iter in range(graph_iter):
            #         curr_edge_masks = torch.stack([torch.unsqueeze(masks[i][edge_iter], dim=0) for i in keep_in_batch])  # seg mask of every prev obj
            #         h_edge = torch.cat((image_feature[keep_in_batch] * curr_edge_masks, image_depth[keep_in_batch] * curr_edge_masks), dim=1)
            #         h_edge_aug = torch.cat((image_feature_aug[keep_in_batch] * curr_edge_masks, image_depth[keep_in_batch] * curr_edge_masks), dim=1)
            #         cat_edge = torch.tensor([torch.unsqueeze(categories[i][edge_iter], dim=0) for i in keep_in_batch]).to(rank)
            #         scat_edge = [super_categories[i][edge_iter] for i in keep_in_batch] if super_categories[0] is not None else None
            #         bbox_edge = torch.stack([bbox[i][edge_iter] for i in keep_in_batch]).to(rank)
            #         iou_mask = torch.ones(len(keep_in_batch), dtype=torch.bool).to(rank)
            #
            #         """
            #         FIRST DIRECTION
            #         """
            #         if args['models']['hierarchical_pred']:
            #             relation_1, relation_2, relation_3, super_relation, connectivity, hidden, hidden_aug \
            #                                         = local_predictor(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge, rank, h_graph_aug, h_edge_aug)
            #             relation = [relation_1, relation_2, relation_3]
            #             hidden_cat = torch.cat((hidden.unsqueeze(1), hidden_aug.unsqueeze(1)), dim=1)
            #
            #             # match with the commonsense filtering pool
            #             relation_pred = torch.hstack((torch.argmax(relation_1, dim=1),
            #                                           torch.argmax(relation_2, dim=1) + args['models']['num_geometric'],
            #                                           torch.argmax(relation_3, dim=1) + args['models']['num_geometric'] + args['models']['num_possessive']))
            #             triplets = torch.hstack((cat_graph.repeat(3).unsqueeze(1), relation_pred.unsqueeze(1), cat_edge.repeat(3).unsqueeze(1)))
            #
            #         else:
            #             relation, connectivity, hidden, hidden_aug = local_predictor(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge, rank, h_graph_aug, h_edge_aug)
            #             hidden_cat = torch.cat((hidden.unsqueeze(1), hidden_aug.unsqueeze(1)), dim=1)
            #             super_relation = None
            #
            #             # match with the commonsense filtering pool
            #             relation_pred = torch.argmax(relation, dim=1)
            #             triplets = torch.hstack((cat_graph.unsqueeze(1), relation_pred.unsqueeze(1), cat_edge.unsqueeze(1)))
            #
            #         # evaluate on the commonsense for all predictions, regardless of whether they match with the ground truth or not
            #         not_in_yes_dict = args['training']['lambda_cs_weak'] * torch.tensor([tuple(triplets[i].cpu().tolist()) not in commonsense_yes_triplets for i in range(len(triplets))], dtype=torch.float).to(rank)
            #         # is_in_no_dict = args['training']['lambda_cs_strong'] * torch.tensor([tuple(triplets[i].cpu().tolist()) in commonsense_no_triplets for i in range(len(triplets))], dtype=torch.float).to(rank)
            #         # loss_commonsense += (not_in_yes_dict + is_in_no_dict).mean()
            #         loss_commonsense += not_in_yes_dict.mean()
            #
            #         # evaluate on the connectivity
            #         not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 1)[0]  # which data samples in curr keep_in_batch are not connected
            #         num_not_connected += len(not_connected)
            #         temp = criterion_connectivity(connectivity[not_connected, 0], torch.zeros(len(not_connected)).to(rank))
            #         loss_connectivity += 0.0 if torch.isnan(temp) else args['training']['lambda_not_connected'] * temp
            #
            #         connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 1)[0]  # which data samples in curr keep_in_batch are connected
            #         num_connected += len(connected)
            #         connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
            #         connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
            #         num_connected_pred += len(connected_pred)
            #
            #         connected_indices = torch.zeros(len(hidden_cat), dtype=torch.bool).to(rank)
            #         hidden_cat = hidden_cat[connected]
            #         connected_indices[connected] = 1
            #         connected_indices_accumulated.append(connected_indices)
            #
            #         # evaluate on the relationships
            #         if len(connected) > 0:
            #             temp = criterion_connectivity(connectivity[connected, 0], torch.ones(len(connected)).to(rank))
            #             loss_connectivity += 0.0 if torch.isnan(temp) else temp
            #             connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))
            #
            #             if args['training']['run_mode'] == 'train_semi':
            #                 loss_relationship += calculate_losses_on_relationships(args, relation, super_relation, connected, relations_target[graph_iter - 1][edge_iter],
            #                                                                        pseudo_label_mask[graph_iter - 1][edge_iter], criterion_relationship, lambda_pseudo)
            #             else:
            #                 loss_relationship += calculate_losses_on_relationships(args, relation, super_relation, connected, relations_target[graph_iter - 1][edge_iter], criterion_relationship)
            #
            #             if args['training']['run_mode'] == 'train_semi':
            #                 curr_pseudo_labels = pseudo_label_mask[graph_iter - 1][edge_iter][connected]
            #                 hidden_cat_labels = relations_target[graph_iter - 1][edge_iter][connected][~curr_pseudo_labels]
            #                 for index, batch_index in enumerate(keep_in_batch[connected][~curr_pseudo_labels]):
            #                     hidden_cat_accumulated[batch_index].append(hidden_cat[~curr_pseudo_labels][index])
            #                     hidden_cat_labels_accumulated[batch_index].append(hidden_cat_labels[index])
            #
            #                 # add consistency regularization for pseudo labels
            #                 # if curr_pseudo_labels.any():
            #                 #     loss_pseudo_consistency += criterion_pseudo_consistency(hidden[connected][curr_pseudo_labels], hidden_aug[connected][curr_pseudo_labels])
            #                 loss_pseudo_consistency += criterion_pseudo_consistency(hidden[connected], hidden_aug[connected])
            #             else:
            #                 hidden_cat_labels = relations_target[graph_iter - 1][edge_iter][connected]
            #                 for index, batch_index in enumerate(keep_in_batch[connected]):
            #                     hidden_cat_accumulated[batch_index].append(hidden_cat[index])
            #                     hidden_cat_labels_accumulated[batch_index].append(hidden_cat_labels[index])
            #
            #         # evaluate recall@k scores
            #         relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
            #         relations_target_directed[not_connected] = -1
            #
            #         if (batch_count % args['training']['eval_freq'] == 0) or (batch_count + 1 == len(train_loader)):
            #             if args['models']['hierarchical_pred']:
            #                 relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
            #             Recall.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
            #                               cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge, iou_mask)
            #             if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
            #                 Recall_top3.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
            #                                        cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge, iou_mask)
            #
            #         losses += loss_relationship \
            #                   + args['training']['lambda_connectivity'] * (loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1)) \
            #                   + args['training']['lambda_commonsense'] * loss_commonsense
            #         running_loss_connectivity += args['training']['lambda_connectivity'] * (
            #                     loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1))
            #         running_loss_relationship += loss_relationship
            #         running_loss_commonsense += args['training']['lambda_commonsense'] * loss_commonsense
            #         if args['training']['run_mode'] == 'train_semi':
            #             losses += args['training']['lambda_pseudo_consistency'] * loss_pseudo_consistency
            #             running_loss_pseudo_consistency += args['training']['lambda_pseudo_consistency'] * loss_pseudo_consistency
            #
            #         """
            #         SECOND DIRECTION
            #         """
            #         if args['models']['hierarchical_pred']:
            #             relation_1, relation_2, relation_3, super_relation, connectivity, hidden2, hidden_aug2 \
            #                                         = local_predictor(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph, rank, h_edge_aug, h_graph_aug)
            #             relation = [relation_1, relation_2, relation_3]
            #             hidden_cat2 = torch.cat((hidden2.unsqueeze(1), hidden_aug2.unsqueeze(1)), dim=1)
            #
            #             # match with the commonsense filtering pool
            #             relation_pred = torch.hstack((torch.argmax(relation_1, dim=1),
            #                                           torch.argmax(relation_2, dim=1) + args['models']['num_geometric'],
            #                                           torch.argmax(relation_3, dim=1) + args['models']['num_geometric'] + args['models']['num_possessive']))
            #             triplets = torch.hstack((cat_edge.repeat(3).unsqueeze(1), relation_pred.unsqueeze(1), cat_graph.repeat(3).unsqueeze(1)))
            #
            #         else:
            #             relation, connectivity, hidden2, hidden_aug2 = local_predictor(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph, rank, h_edge_aug, h_graph_aug)
            #             hidden_cat2 = torch.cat((hidden2.unsqueeze(1), hidden_aug2.unsqueeze(1)), dim=1)
            #             super_relation = None
            #
            #             # match with the commonsense filtering pool
            #             relation_pred = torch.argmax(relation, dim=1)
            #             triplets = torch.hstack((cat_edge.unsqueeze(1), relation_pred.unsqueeze(1), cat_graph.unsqueeze(1)))
            #
            #         # evaluate on the commonsense for all predictions, regardless of whether they match with the ground truth or not
            #         not_in_yes_dict = args['training']['lambda_cs_weak'] * torch.tensor([tuple(triplets[i].cpu().tolist()) not in commonsense_yes_triplets for i in range(len(triplets))], dtype=torch.float).to(rank)
            #         # is_in_no_dict = args['training']['lambda_cs_strong'] * torch.tensor([tuple(triplets[i].cpu().tolist()) in commonsense_no_triplets for i in range(len(triplets))], dtype=torch.float).to(rank)
            #         # loss_commonsense += (not_in_yes_dict + is_in_no_dict).mean()
            #         loss_commonsense += not_in_yes_dict.mean()
            #
            #         # evaluate on the connectivity
            #         not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 0)[0]  # which data samples in curr keep_in_batch are not connected
            #         num_not_connected += len(not_connected)
            #         temp = criterion_connectivity(connectivity[not_connected, 0], torch.zeros(len(not_connected)).to(rank))
            #         loss_connectivity += 0.0 if torch.isnan(temp) else args['training']['lambda_not_connected'] * temp
            #
            #         connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 0)[0]  # which data samples in curr keep_in_batch are connected
            #         num_connected += len(connected)
            #         connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
            #         connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
            #         num_connected_pred += len(connected_pred)
            #
            #         connected_indices = torch.zeros(len(hidden_cat2), dtype=torch.bool).to(rank)
            #         hidden_cat2 = hidden_cat2[connected]
            #         connected_indices[connected] = 1
            #         connected_indices_accumulated.append(connected_indices)
            #
            #         # evaluate on the relationships
            #         if len(connected) > 0:
            #             temp = criterion_connectivity(connectivity[connected, 0], torch.ones(len(connected)).to(rank))
            #             loss_connectivity += 0.0 if torch.isnan(temp) else temp
            #             connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))
            #
            #             if args['training']['run_mode'] == 'train_semi':
            #                 loss_relationship += calculate_losses_on_relationships(args, relation, super_relation, connected, relations_target[graph_iter - 1][edge_iter],
            #                                                                        pseudo_label_mask[graph_iter - 1][edge_iter], criterion_relationship, lambda_pseudo)
            #             else:
            #                 loss_relationship += calculate_losses_on_relationships(args, relation, super_relation, connected, relations_target[graph_iter - 1][edge_iter], criterion_relationship)
            #
            #             if args['training']['run_mode'] == 'train_semi':
            #                 curr_pseudo_labels = pseudo_label_mask[graph_iter - 1][edge_iter][connected]
            #                 hidden_cat_labels2 = relations_target[graph_iter - 1][edge_iter][connected][~curr_pseudo_labels]
            #                 for index, batch_index in enumerate(keep_in_batch[connected][~curr_pseudo_labels]):
            #                     hidden_cat_accumulated[batch_index].append(hidden_cat2[~curr_pseudo_labels][index])
            #                     hidden_cat_labels_accumulated[batch_index].append(hidden_cat_labels2[index])
            #
            #                 # add consistency regularization for pseudo labels
            #                 loss_pseudo_consistency += criterion_pseudo_consistency(hidden[connected], hidden_aug[connected])
            #                 # if curr_pseudo_labels.any():
            #                 #     loss_pseudo_consistency += criterion_pseudo_consistency(hidden[connected][curr_pseudo_labels], hidden_aug[connected][curr_pseudo_labels])
            #             else:
            #                 hidden_cat_labels2 = relations_target[graph_iter - 1][edge_iter][connected]
            #                 for index, batch_index in enumerate(keep_in_batch[connected]):
            #                     hidden_cat_accumulated[batch_index].append(hidden_cat2[index])
            #                     hidden_cat_labels_accumulated[batch_index].append(hidden_cat_labels2[index])
            #
            #         # evaluate recall@k scores
            #         relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
            #         relations_target_directed[not_connected] = -1
            #
            #         if (batch_count % args['training']['eval_freq'] == 0) or (batch_count + 1 == len(train_loader)):
            #             if args['models']['hierarchical_pred']:
            #                 relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
            #             Recall.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
            #                               cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge, iou_mask)
            #             if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
            #                 Recall_top3.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
            #                                        cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge, iou_mask)
            #
            #         losses += loss_relationship \
            #                   + args['training']['lambda_connectivity'] * (loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1)) \
            #                   + args['training']['lambda_commonsense'] * loss_commonsense
            #         running_loss_connectivity += args['training']['lambda_connectivity'] * (
            #                 loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1))
            #         running_loss_relationship += loss_relationship
            #         running_loss_commonsense += args['training']['lambda_commonsense'] * loss_commonsense
            #         if args['training']['run_mode'] == 'train_semi':
            #             losses += args['training']['lambda_pseudo_consistency'] * loss_pseudo_consistency
            #             running_loss_pseudo_consistency += args['training']['lambda_pseudo_consistency'] * loss_pseudo_consistency
            #
            # if not all(len(sublist) == 0 for sublist in hidden_cat_accumulated):
            #     # concatenate all hidden_cat and hidden_cat_labels along the 0th dimension
            #     hidden_cat_accumulated = [torch.stack(sublist) for sublist in hidden_cat_accumulated if len(sublist) > 0]
            #     hidden_cat_labels_accumulated = [torch.stack(sublist) for sublist in hidden_cat_labels_accumulated if len(sublist) > 0]
            #
            #     hidden_cat_all = torch.cat(hidden_cat_accumulated, dim=0)
            #     hidden_cat_labels_all = torch.cat(hidden_cat_labels_accumulated, dim=0)
            #
            #     temp = criterion_contrast(rank, hidden_cat_all, hidden_cat_labels_all)
            #     loss_contrast += 0.0 if torch.isnan(temp) else args['training']['lambda_contrast'] * temp
            #
            # running_loss_contrast += args['training']['lambda_contrast'] * loss_contrast
            # losses += args['training']['lambda_contrast'] * loss_contrast
            # running_losses += losses.item()
            #
            # optimizer.zero_grad()
            # losses.backward()
            # optimizer.step()
            #
            # if rank == 0:
            #     global_step = batch_count + len(train_loader) * epoch
            #     writer.add_scalar('train/running_loss_relationship', running_loss_relationship, global_step)
            #     writer.add_scalar('train/running_loss_connectivity', running_loss_connectivity, global_step)
            #     writer.add_scalar('train/running_loss_contrast', running_loss_contrast, global_step)
            #     writer.add_scalar('train/running_losses', running_losses, global_step)
            #     writer.add_scalar('train/running_loss_commonsense', running_loss_commonsense, global_step)
            #     if args['training']['run_mode'] == 'train_semi':
            #         writer.add_scalar('train/running_loss_pseudo_consistency', running_loss_pseudo_consistency, global_step)
            #
            # """
            # EVALUATE AND PRINT CURRENT TRAINING RESULTS
            # """
            # if (batch_count % args['training']['eval_freq'] == 0) or (batch_count + 1 == len(train_loader)):
            #     recall_top3, mean_recall_top3 = None, None
            #     if args['dataset']['dataset'] == 'vg':
            #         recall, _, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True)
            #         if args['models']['hierarchical_pred']:
            #             recall_top3, _, mean_recall_top3 = Recall_top3.compute(per_class=True)
            #             Recall_top3.clear_data()
            #     else:
            #         recall, _, mean_recall, _, _, _ = Recall.compute(per_class=True)
            #         wmap_rel, wmap_phrase = Recall.compute_precision()
            #     Recall.clear_data()
            #
            # if (batch_count % args['training']['print_freq'] == 0) or (batch_count + 1 == len(train_loader)):
            #     record_train_results(args, record, rank, epoch, batch_count, optimizer.param_groups[0]['lr'], recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs,
            #                          running_losses, running_loss_relationship, running_loss_contrast, running_loss_connectivity, running_loss_pseudo_consistency, running_loss_commonsense,
            #                          connectivity_recall, num_connected, num_not_connected, connectivity_precision, num_connected_pred, wmap_rel, wmap_phrase)
            #     dist.monitored_barrier()
            #
            # running_losses, running_loss_connectivity, running_loss_relationship, running_loss_contrast, running_loss_pseudo_consistency, running_loss_commonsense, \
            #     connectivity_precision, num_connected, num_not_connected = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

        if epoch == 0:
            mean_num_rel_before, mean_num_rel_after = train_dataset.calculate_mean_num_rel_before_after_semi()
            all_triplets = train_dataset.get_triplets()
            print('Mean number of relations before and after semi-supervised training: %.4f' % mean_num_rel_before, '%.4f' % mean_num_rel_after)

        # if args['models']['hierarchical_pred']:
        #     torch.save(local_predictor.state_dict(), args['training']['checkpoint_path'] + 'HierMotif_CS' + str(epoch) + '_' + str(rank) + '.pth')
        # else:
        #     torch.save(local_predictor.state_dict(), args['training']['checkpoint_path'] + 'FlatMotif_Semi' + str(epoch) + '_' + str(rank) + '.pth')
        # dist.monitored_barrier()
        #
        # test_local(args, detr, local_predictor, test_loader, test_record, epoch, rank, writer, test_dataset)

    dist.destroy_process_group()  # clean up
    if rank == 0:
        writer.close()
    print('FINISHED TRAINING\n')



def test_local(args, backbone, local_predictor, test_loader, test_record, epoch, rank, writer, test_dataset):
    backbone.eval()
    local_predictor.eval()

    connectivity_recall, connectivity_precision, num_connected, num_not_connected, num_connected_pred = 0.0, 0.0, 0.0, 0.0, 0.0
    recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs, wmap_rel, wmap_phrase = None, None, None, None, None, None, None
    Recall = Evaluator_PC(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
    if args['dataset']['dataset'] == 'vg':
        Recall_top3 = Evaluator_PC_Top3(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])

    print('Start Testing PC...')
    with torch.no_grad():
        for batch_count, data in enumerate(tqdm(test_loader), 0):
            """
            PREPARE INPUT DATA
            """
            try:
                images, _, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, _ = data
            except:
                continue

            images = torch.stack(images).to(rank)
            image_feature, pos_embed = backbone.module.backbone(nested_tensor_from_tensor_list(images))
            src, mask = image_feature[-1].decompose()
            src = backbone.module.input_proj(src).flatten(2).permute(2, 0, 1)
            pos_embed = pos_embed[-1].flatten(2).permute(2, 0, 1)
            image_feature = backbone.module.transformer.encoder(src, src_key_padding_mask=mask.flatten(1), pos=pos_embed)
            image_feature = image_feature.permute(1, 2, 0)
            image_feature = image_feature.view(-1, args['models']['num_img_feature'], args['models']['feature_size'], args['models']['feature_size'])
            del images

            categories = [category.to(rank) for category in categories]  # [batch_size][curr_num_obj, 1]
            if super_categories[0] is not None:
                super_categories = [[sc.to(rank) for sc in super_category] for super_category in super_categories]  # [batch_size][curr_num_obj, [1 or more]]
            image_depth = torch.stack([depth.to(rank) for depth in image_depth])
            bbox = [box.to(rank) for box in bbox]  # [batch_size][curr_num_obj, 4]

            masks = []
            for i in range(len(bbox)):
                mask = torch.zeros(bbox[i].shape[0], args['models']['feature_size'], args['models']['feature_size'], dtype=torch.bool).to(rank)
                for j, box in enumerate(bbox[i]):
                    mask[j, int(bbox[i][j][2]):int(bbox[i][j][3]), int(bbox[i][j][0]):int(bbox[i][j][1])] = 1
                masks.append(mask)

            """
            PREPARE TARGETS
            """
            relations_target = []
            direction_target = []
            num_graph_iter = torch.as_tensor([len(mask) for mask in masks]) - 1
            for graph_iter in range(max(num_graph_iter)):
                keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)
                relations_target.append(torch.vstack([relationships[i][graph_iter] for i in keep_in_batch]).T.to(rank))  # integer labels
                direction_target.append(torch.vstack([subj_or_obj[i][graph_iter] for i in keep_in_batch]).T.to(rank))

            """
            FORWARD PASS THROUGH THE LOCAL PREDICTOR
            """
            num_graph_iter = torch.as_tensor([len(mask) for mask in masks])
            for graph_iter in range(max(num_graph_iter)):
                keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1).to(rank)

                curr_graph_masks = torch.stack([torch.unsqueeze(masks[i][graph_iter], dim=0) for i in keep_in_batch])
                h_graph = torch.cat((image_feature[keep_in_batch] * curr_graph_masks, image_depth[keep_in_batch] * curr_graph_masks), dim=1)  # (bs, 256, 64, 64), (bs, 1, 64, 64)
                cat_graph = torch.tensor([torch.unsqueeze(categories[i][graph_iter], dim=0) for i in keep_in_batch]).to(rank)
                scat_graph = [super_categories[i][graph_iter] for i in keep_in_batch] if super_categories[0] is not None else None
                bbox_graph = torch.stack([bbox[i][graph_iter] for i in keep_in_batch]).to(rank)

                for edge_iter in range(graph_iter):
                    curr_edge_masks = torch.stack([torch.unsqueeze(masks[i][edge_iter], dim=0) for i in keep_in_batch])  # seg mask of every prev obj
                    h_edge = torch.cat((image_feature[keep_in_batch] * curr_edge_masks, image_depth[keep_in_batch] * curr_edge_masks), dim=1)
                    cat_edge = torch.tensor([torch.unsqueeze(categories[i][edge_iter], dim=0) for i in keep_in_batch]).to(rank)
                    scat_edge = [super_categories[i][edge_iter] for i in keep_in_batch] if super_categories[0] is not None else None
                    bbox_edge = torch.stack([bbox[i][edge_iter] for i in keep_in_batch]).to(rank)

                    # filter out subject-object pairs whose iou=0
                    joint_intersect = torch.logical_or(curr_graph_masks, curr_edge_masks)
                    joint_union = torch.logical_and(curr_graph_masks, curr_edge_masks)
                    joint_iou = (torch.sum(torch.sum(joint_intersect, dim=-1), dim=-1) / torch.sum(torch.sum(joint_union, dim=-1), dim=-1)).flatten()
                    joint_iou[torch.isinf(joint_iou)] = 0
                    iou_mask = joint_iou > 0
                    if torch.sum(iou_mask) == 0:
                        continue
                    # iou_mask = torch.ones(len(iou_mask), dtype=torch.bool).to(rank)

                    """
                    FIRST DIRECTION
                    """
                    if args['models']['hierarchical_pred']:
                        relation_1, relation_2, relation_3, super_relation, connectivity, _, _ = local_predictor(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge, rank)
                        relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
                    else:
                        relation, connectivity, _, _ = local_predictor(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge, rank)
                        super_relation = None

                    not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 1)[0]  # which data samples in curr keep_in_batch are not connected
                    num_not_connected += len(not_connected)
                    connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 1)[0]  # which data samples in curr keep_in_batch are connected
                    num_connected += len(connected)
                    connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
                    connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
                    num_connected_pred += len(connected_pred)

                    if len(connected) > 0:
                        connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))

                    # evaluate recall@k scores
                    relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
                    relations_target_directed[not_connected] = -1

                    if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                        Recall.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                          cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge, iou_mask)
                        if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
                            Recall_top3.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                                   cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge, iou_mask)

                    """
                    SECOND DIRECTION
                    """
                    if args['models']['hierarchical_pred']:
                        relation_1, relation_2, relation_3, super_relation, connectivity, _, _ = local_predictor(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph, rank)
                        relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
                    else:
                        relation, connectivity, _, _ = local_predictor(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph, rank)
                        super_relation = None

                    not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 0)[0]  # which data samples in curr keep_in_batch are not connected
                    num_not_connected += len(not_connected)
                    connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 0)[0]  # which data samples in curr keep_in_batch are connected
                    num_connected += len(connected)
                    connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
                    connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
                    num_connected_pred += len(connected_pred)

                    if len(connected) > 0:
                        connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))

                    relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
                    relations_target_directed[not_connected] = -1

                    if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                        Recall.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                          cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge, iou_mask)
                        if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
                            Recall_top3.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                                   cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge, iou_mask)

            """
            EVALUATE AND PRINT CURRENT RESULTS
            """
            if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                recall_top3, mean_recall_top3 = None, None
                # top_k_predictions, top_k_image_graph = Recall.get_top_k_predictions(20)
                # print('top_k_predictions', top_k_predictions)
                if args['dataset']['dataset'] == 'vg':
                    recall, _, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True)
                    if rank == 0:
                        global_step = batch_count + len(test_loader) * epoch
                        writer.add_scalar('test/Recall@20', recall[0], global_step)
                        writer.add_scalar('test/Recall@50', recall[1], global_step)
                        writer.add_scalar('test/Recall@100', recall[2], global_step)
                    if args['models']['hierarchical_pred']:
                        recall_top3, _, mean_recall_top3 = Recall_top3.compute(per_class=True)
                        Recall_top3.clear_data()
                else:
                    recall, _, mean_recall, _, _, _ = Recall.compute(per_class=True)
                    wmap_rel, wmap_phrase = Recall.compute_precision()
                Recall.clear_data()

            if (batch_count % args['training']['print_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                record_test_results(args, test_record, rank, epoch, recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs,
                                    connectivity_recall, num_connected, num_not_connected, connectivity_precision, num_connected_pred, wmap_rel, wmap_phrase)
                dist.monitored_barrier()

        # if epoch == 0:
        #     all_triplets = test_dataset.get_triplets()
    print('FINISHED EVALUATING\n')


# # import torch
# # import torch.nn as nn
# # import torch.nn.functional as F
# # import torch.optim as optim
# # import numpy as np
# # from tqdm import tqdm
# # import json
# # import os
# # import math
# # import torchvision
# # from torchvision import transforms
# # import torch.distributed as dist
# # from torch.nn.parallel import DistributedDataParallel as DDP
# # from torch.utils.tensorboard import SummaryWriter
# # import shutil
# #
# # from evaluator import Evaluator_PC, Evaluator_PC_Top3
# # from model import *
# # from utils import *
# # from sup_contrast.losses import SupConLoss, SupConLossHierar
# #
# #
# # def setup(rank, world_size):
# #     os.environ['MASTER_ADDR'] = 'localhost'
# #     os.environ['MASTER_PORT'] = '12355'
# #     dist.init_process_group("gloo", rank=rank, world_size=world_size)
# #
# #
# # def train_local(gpu, args, train_subset, test_subset):
# #     """
# #     This function trains and evaluates the local prediction module on predicate classification tasks.
# #     :param gpu: current gpu index
# #     :param args: input arguments in config.yaml
# #     :param train_subset: training dataset
# #     :param test_subset: testing dataset
# #     """
# #     rank = gpu
# #     world_size = torch.cuda.device_count()
# #     setup(rank, world_size)
# #     print('rank', rank, 'torch.distributed.is_initialized', torch.distributed.is_initialized())
# #
# #     if rank == 0:
# #         log_dir = 'runs/train_sg'
# #         if os.path.exists(log_dir):
# #             shutil.rmtree(log_dir)  # remove the old log directory if it exists
# #         writer = SummaryWriter(log_dir)
# #
# #     train_sampler = torch.utils.data.distributed.DistributedSampler(train_subset, num_replicas=world_size, rank=rank)
# #     train_loader = torch.utils.data.DataLoader(train_subset, batch_size=args['training']['batch_size'], shuffle=False, collate_fn=collate_fn, num_workers=0, drop_last=True, sampler=train_sampler)
# #     test_sampler = torch.utils.data.distributed.DistributedSampler(test_subset, num_replicas=world_size, rank=rank)
# #     test_loader = torch.utils.data.DataLoader(test_subset, batch_size=args['training']['batch_size'], shuffle=False, collate_fn=collate_fn, num_workers=0, drop_last=True, sampler=test_sampler)
# #     print("Finished loading the datasets...")
# #
# #     start = []
# #     if not args['training']['continue_train']:
# #         record = []
# #         test_record = []
# #         with open(args['training']['result_path'] + 'train_results_' + str(rank) + '.json', 'w') as f:  # clear history logs
# #             json.dump(start, f)
# #         with open(args['training']['result_path'] + 'test_results_' + str(rank) + '.json', 'w') as f:  # clear history logs
# #             json.dump(start, f)
# #     else:
# #         with open(args['training']['result_path'] + 'train_results_' + str(rank) + '.json', 'r') as f:
# #             record = json.load(f)
# #         with open(args['training']['result_path'] + 'test_results_' + str(rank) + '.json', 'r') as f:
# #             test_record = json.load(f)
# #
# #     if args['models']['hierarchical_pred']:
# #         local_predictor = DDP(HierMotif(args=args, input_dim=args['models']['hidden_dim'], feature_size=args['models']['feature_size'],
# #                                         num_classes=args['models']['num_classes'], num_super_classes=args['models']['num_super_classes'],
# #                                         num_geometric=args['models']['num_geometric'], num_possessive=args['models']['num_possessive'],
# #                                         num_semantic=args['models']['num_semantic'])).to(rank)
# #     else:
# #         local_predictor = DDP(FlatMotif(args=args, input_dim=args['models']['hidden_dim'], output_dim=args['models']['num_relations'], feature_size=args['models']['feature_size'],
# #                                         num_classes=args['models']['num_classes'])).to(rank)
# #
# #     if args['models']['detr_or_faster_rcnn'] == 'detr':
# #         detr = DDP(build_detr101(args)).to(rank)
# #         detr.eval()
# #     else:
# #         print('Unknown model.')
# #
# #     map_location = {'cuda:%d' % rank: 'cuda:%d' % 0}
# #     if args['training']['continue_train']:
# #         if args['models']['hierarchical_pred']:
# #             local_predictor.load_state_dict(torch.load(args['training']['checkpoint_path'] + 'HierMotif_4' + str(args['training']['start_epoch'] - 1) + '.pth', map_location=map_location))
# #         else:
# #             local_predictor.load_state_dict(torch.load(args['training']['checkpoint_path'] + 'FlatMotif_4' + str(args['training']['start_epoch'] - 1) + '.pth', map_location=map_location))
# #
# #     optimizer = optim.SGD([{'params': local_predictor.parameters(), 'initial_lr': args['training']['learning_rate']}],
# #                           lr=args['training']['learning_rate'], momentum=0.9, weight_decay=args['training']['weight_decay'])
# #     local_predictor.train()
# #
# #     original_lr = optimizer.param_groups[0]["lr"]
# #     relation_count = get_num_each_class_reordered(args)
# #     class_weight = 1 - relation_count / torch.sum(relation_count)
# #
# #     if args['models']['hierarchical_pred']:
# #         criterion_relationship_1 = torch.nn.NLLLoss(weight=class_weight[:args['models']['num_geometric']].to(rank))  # log softmax already applied
# #         criterion_relationship_2 = torch.nn.NLLLoss(weight=class_weight[args['models']['num_geometric']:args['models']['num_geometric']+args['models']['num_possessive']].to(rank))
# #         criterion_relationship_3 = torch.nn.NLLLoss(weight=class_weight[args['models']['num_geometric']+args['models']['num_possessive']:].to(rank))
# #         criterion_super_relationship = torch.nn.NLLLoss()
# #     else:
# #         criterion_relationship = torch.nn.CrossEntropyLoss(weight=class_weight.to(rank))
# #     criterion_contrast = SupConLossHierar()
# #     criterion_connectivity = torch.nn.BCEWithLogitsLoss()
# #
# #     running_losses, running_loss_connectivity, running_loss_relationship, running_loss_contrast, connectivity_recall, connectivity_precision, \
# #         num_connected, num_not_connected, num_connected_pred = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
# #     recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs, wmap_rel, wmap_phrase = None, None, None, None, None, None, None, None
# #
# #     Recall = Evaluator_PC(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
# #     if args['dataset']['dataset'] == 'vg':
# #         Recall_top3 = Evaluator_PC_Top3(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
# #
# #     lr_decay = 1
# #     # desired_batch_size = 32
# #     # current_batch_size = 8
# #     # accumulation_steps = desired_batch_size / current_batch_size
# #     # optimizer.zero_grad()  # Reset gradients tensors at the beginning of the epoch
# #
# #     for epoch in range(args['training']['start_epoch'], args['training']['num_epoch']):
# #         print('Start Training... EPOCH %d / %d\n' % (epoch, args['training']['num_epoch']))
# #         if epoch == args['training']['scheduler_param1'] or epoch == args['training']['scheduler_param2']:  # lr scheduler
# #             lr_decay *= 0.1
# #
# #         for batch_count, data in enumerate(tqdm(train_loader), 0):
# #             """
# #             PREPARE INPUT DATA
# #             """
# #             try:
# #                 images, images_aug, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, _ = data
# #             except:
# #                 continue
# #             batch_size = len(images)
# #
# #             with torch.no_grad():
# #                 images = torch.stack(images).to(rank)
# #                 image_feature, pos_embed = detr.module.backbone(nested_tensor_from_tensor_list(images))
# #                 src, mask = image_feature[-1].decompose()
# #                 src = detr.module.input_proj(src).flatten(2).permute(2, 0, 1)
# #                 pos_embed = pos_embed[-1].flatten(2).permute(2, 0, 1)
# #                 image_feature = detr.module.transformer.encoder(src, src_key_padding_mask=mask.flatten(1), pos=pos_embed)
# #                 image_feature = image_feature.permute(1, 2, 0)
# #                 image_feature = image_feature.view(-1, args['models']['num_img_feature'], args['models']['feature_size'], args['models']['feature_size'])
# #
# #                 images_aug = torch.stack(images_aug).to(rank)
# #                 image_feature_aug, pos_embed = detr.module.backbone(nested_tensor_from_tensor_list(images_aug))
# #                 src, mask = image_feature_aug[-1].decompose()
# #                 src = detr.module.input_proj(src).flatten(2).permute(2, 0, 1)
# #                 pos_embed = pos_embed[-1].flatten(2).permute(2, 0, 1)
# #                 image_feature_aug = detr.module.transformer.encoder(src, src_key_padding_mask=mask.flatten(1), pos=pos_embed)
# #                 image_feature_aug = image_feature_aug.permute(1, 2, 0)
# #                 image_feature_aug = image_feature_aug.view(-1, args['models']['num_img_feature'], args['models']['feature_size'], args['models']['feature_size'])
# #                 del images, images_aug
# #
# #             categories = [category.to(rank) for category in categories]  # [batch_size][curr_num_obj, 1]
# #             if super_categories[0] is not None:
# #                 super_categories = [[sc.to(rank) for sc in super_category] for super_category in super_categories]  # [batch_size][curr_num_obj, [1 or more]]
# #             image_depth = torch.stack([depth.to(rank) for depth in image_depth])
# #             bbox = [box.to(rank) for box in bbox]  # [batch_size][curr_num_obj, 4]
# #             optimizer.param_groups[0]["lr"] = original_lr
# #
# #             masks = []
# #             for i in range(len(bbox)):
# #                 mask = torch.zeros(bbox[i].shape[0], args['models']['feature_size'], args['models']['feature_size'], dtype=torch.bool).to(rank)
# #                 for j, box in enumerate(bbox[i]):
# #                     mask[j, int(bbox[i][j][2]):int(bbox[i][j][3]), int(bbox[i][j][0]):int(bbox[i][j][1])] = 1
# #                 masks.append(mask)
# #
# #             """
# #             PREPARE TARGETS
# #             """
# #             relations_target = []
# #             direction_target = []
# #             num_graph_iter = torch.as_tensor([len(mask) for mask in masks]) - 1
# #             for graph_iter in range(max(num_graph_iter)):
# #                 keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)
# #                 relations_target.append(torch.vstack([relationships[i][graph_iter] for i in keep_in_batch]).T.to(rank))  # integer labels
# #                 direction_target.append(torch.vstack([subj_or_obj[i][graph_iter] for i in keep_in_batch]).T.to(rank))
# #
# #             """
# #             FORWARD PASS
# #             """
# #             hidden_cat_accumulated = [[] for _ in range(batch_size)]
# #             hidden_cat_labels_accumulated = [[] for _ in range(batch_size)]
# #             connected_indices_accumulated = []
# #             losses, loss_connectivity, loss_relationship, loss_contrast = 0.0, 0.0, 0.0, 0.0
# #
# #             num_graph_iter = torch.as_tensor([len(mask) for mask in masks])
# #             for graph_iter in range(max(num_graph_iter)):
# #                 keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1).to(rank)
# #                 optimizer.param_groups[0]["lr"] = original_lr * lr_decay #* math.sqrt(len(keep_in_batch) / len(num_graph_iter))  # dynamic batch size needs dynamic learning rate
# #
# #                 curr_graph_masks = torch.stack([torch.unsqueeze(masks[i][graph_iter], dim=0) for i in keep_in_batch])
# #                 h_graph = torch.cat((image_feature[keep_in_batch] * curr_graph_masks, image_depth[keep_in_batch] * curr_graph_masks), dim=1)  # (bs, 256, 64, 64), (bs, 1, 64, 64)
# #                 h_graph_aug = torch.cat((image_feature_aug[keep_in_batch] * curr_graph_masks, image_depth[keep_in_batch] * curr_graph_masks), dim=1)
# #                 cat_graph = torch.tensor([torch.unsqueeze(categories[i][graph_iter], dim=0) for i in keep_in_batch]).to(rank)
# #                 scat_graph = [super_categories[i][graph_iter] for i in keep_in_batch] if super_categories[0] is not None else None
# #                 bbox_graph = torch.stack([bbox[i][graph_iter] for i in keep_in_batch]).to(rank)
# #
# #                 for edge_iter in range(graph_iter):
# #                     curr_edge_masks = torch.stack([torch.unsqueeze(masks[i][edge_iter], dim=0) for i in keep_in_batch])  # seg mask of every prev obj
# #                     h_edge = torch.cat((image_feature[keep_in_batch] * curr_edge_masks, image_depth[keep_in_batch] * curr_edge_masks), dim=1)
# #                     h_edge_aug = torch.cat((image_feature_aug[keep_in_batch] * curr_edge_masks, image_depth[keep_in_batch] * curr_edge_masks), dim=1)
# #                     cat_edge = torch.tensor([torch.unsqueeze(categories[i][edge_iter], dim=0) for i in keep_in_batch]).to(rank)
# #                     scat_edge = [super_categories[i][edge_iter] for i in keep_in_batch] if super_categories[0] is not None else None
# #                     bbox_edge = torch.stack([bbox[i][edge_iter] for i in keep_in_batch]).to(rank)
# #
# #                     """
# #                     FIRST DIRECTION
# #                     """
# #                     if args['models']['hierarchical_pred']:
# #                         relation_1, relation_2, relation_3, super_relation, connectivity, hidden, hidden_aug = local_predictor(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge,
# #                                                                                                                                rank, h_graph_aug, h_edge_aug)
# #                         hidden_cat = torch.cat((hidden.unsqueeze(1), hidden_aug.unsqueeze(1)), dim=1)
# #                         relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
# #                     else:
# #                         relation, connectivity, hidden, hidden_aug = local_predictor(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge, rank, h_graph_aug, h_edge_aug)
# #                         hidden_cat = torch.cat((hidden.unsqueeze(1), hidden_aug.unsqueeze(1)), dim=1)
# #                         super_relation = None
# #
# #                     not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 1)[0]  # which data samples in curr keep_in_batch are not connected
# #                     num_not_connected += len(not_connected)
# #                     temp = criterion_connectivity(connectivity[not_connected, 0], torch.zeros(len(not_connected)).to(rank))
# #                     loss_connectivity += 0.0 if torch.isnan(temp) else args['training']['lambda_not_connected'] * temp
# #
# #                     connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 1)[0]  # which data samples in curr keep_in_batch are connected
# #                     num_connected += len(connected)
# #                     connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
# #                     connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
# #                     num_connected_pred += len(connected_pred)
# #
# #                     connected_indices = torch.zeros(len(hidden_cat), dtype=torch.bool).to(rank)
# #                     hidden_cat = hidden_cat[connected]
# #                     connected_indices[connected] = 1
# #                     connected_indices_accumulated.append(connected_indices)
# #
# #                     if len(connected) > 0:
# #                         temp = criterion_connectivity(connectivity[connected, 0], torch.ones(len(connected)).to(rank))
# #                         loss_connectivity += 0.0 if torch.isnan(temp) else temp
# #                         connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))
# #
# #                         if args['models']['hierarchical_pred']:
# #                             super_relation_target = relations_target[graph_iter - 1][edge_iter][connected].clone()
# #                             super_relation_target[super_relation_target < args['models']['num_geometric']] = 0
# #                             super_relation_target[torch.logical_and(super_relation_target >= args['models']['num_geometric'], super_relation_target < args['models']['num_geometric']+args['models']['num_possessive'])] = 1
# #                             super_relation_target[super_relation_target >= args['models']['num_geometric']+args['models']['num_possessive']] = 2
# #                             loss_relationship += criterion_super_relationship(super_relation[connected], super_relation_target)
# #
# #                             connected_1 = torch.nonzero(relations_target[graph_iter - 1][edge_iter][connected] < args['models']['num_geometric']).flatten()  # geometric
# #                             connected_2 = torch.nonzero(torch.logical_and(relations_target[graph_iter - 1][edge_iter][connected] >= args['models']['num_geometric'],
# #                                                                           relations_target[graph_iter - 1][edge_iter][connected] < args['models']['num_geometric']+args['models']['num_possessive'])).flatten()  # possessive
# #                             connected_3 = torch.nonzero(relations_target[graph_iter - 1][edge_iter][connected] >= args['models']['num_geometric']+args['models']['num_possessive']).flatten()  # semantic
# #                             if len(connected_1) > 0:
# #                                 loss_relationship += criterion_relationship_1(relation_1[connected][connected_1], relations_target[graph_iter - 1][edge_iter][connected][connected_1])
# #                             if len(connected_2) > 0:
# #                                 loss_relationship += criterion_relationship_2(relation_2[connected][connected_2], relations_target[graph_iter - 1][edge_iter][connected][connected_2] - args['models']['num_geometric'])
# #                             if len(connected_3) > 0:
# #                                 loss_relationship += criterion_relationship_3(relation_3[connected][connected_3], relations_target[graph_iter - 1][edge_iter][connected][connected_3] - args['models']['num_geometric'] - args['models']['num_possessive'])
# #                         else:
# #                             loss_relationship += criterion_relationship(relation[connected], relations_target[graph_iter - 1][edge_iter][connected])
# #
# #                         hidden_cat_labels = relations_target[graph_iter - 1][edge_iter][connected]
# #                         for index, batch_index in enumerate(keep_in_batch[connected]):
# #                             hidden_cat_accumulated[batch_index].append(hidden_cat[index])
# #                             hidden_cat_labels_accumulated[batch_index].append(hidden_cat_labels[index])
# #
# #                     # evaluate recall@k scores
# #                     relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
# #                     relations_target_directed[not_connected] = -1
# #
# #                     if (batch_count % args['training']['eval_freq'] == 0) or (batch_count + 1 == len(train_loader)):
# #                         Recall.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
# #                                           cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
# #                         if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
# #                             Recall_top3.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
# #                                                    cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
# #
# #                     losses += loss_relationship + args['training']['lambda_connectivity'] * (
# #                                 loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1))
# #                     running_loss_connectivity += args['training']['lambda_connectivity'] * (
# #                                 loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1))
# #                     running_loss_relationship += loss_relationship
# #
# #                     """
# #                     SECOND DIRECTION
# #                     """
# #                     if args['models']['hierarchical_pred']:
# #                         relation_1, relation_2, relation_3, super_relation, connectivity, hidden2, hidden_aug2 = local_predictor(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph,
# #                                                                                                                                  rank, h_edge_aug, h_graph_aug)
# #                         relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
# #                         hidden_cat2 = torch.cat((hidden2.unsqueeze(1), hidden_aug2.unsqueeze(1)), dim=1)
# #                     else:
# #                         relation, connectivity, hidden2, hidden_aug2 = local_predictor(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph, rank, h_edge_aug, h_graph_aug)
# #                         hidden_cat2 = torch.cat((hidden2.unsqueeze(1), hidden_aug2.unsqueeze(1)), dim=1)
# #                         super_relation = None
# #
# #                     not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 0)[0]  # which data samples in curr keep_in_batch are not connected
# #                     num_not_connected += len(not_connected)
# #                     temp = criterion_connectivity(connectivity[not_connected, 0], torch.zeros(len(not_connected)).to(rank))
# #                     loss_connectivity += 0.0 if torch.isnan(temp) else args['training']['lambda_not_connected'] * temp
# #
# #                     connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 0)[0]  # which data samples in curr keep_in_batch are connected
# #                     num_connected += len(connected)
# #                     connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
# #                     connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
# #                     num_connected_pred += len(connected_pred)
# #
# #                     connected_indices = torch.zeros(len(hidden_cat2), dtype=torch.bool).to(rank)
# #                     hidden_cat2 = hidden_cat2[connected]
# #                     connected_indices[connected] = 1
# #                     connected_indices_accumulated.append(connected_indices)
# #
# #                     if len(connected) > 0:
# #                         temp = criterion_connectivity(connectivity[connected, 0], torch.ones(len(connected)).to(rank))
# #                         loss_connectivity += 0.0 if torch.isnan(temp) else temp
# #                         connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))
# #
# #                         if args['models']['hierarchical_pred']:
# #                             super_relation_target = relations_target[graph_iter - 1][edge_iter][connected].clone()
# #                             super_relation_target[super_relation_target < args['models']['num_geometric']] = 0
# #                             super_relation_target[torch.logical_and(super_relation_target >= args['models']['num_geometric'], super_relation_target < args['models']['num_geometric']+args['models']['num_possessive'])] = 1
# #                             super_relation_target[super_relation_target >= args['models']['num_geometric']+args['models']['num_possessive']] = 2
# #                             loss_relationship += criterion_super_relationship(super_relation[connected], super_relation_target)
# #
# #                             connected_1 = torch.nonzero(relations_target[graph_iter - 1][edge_iter][connected] < args['models']['num_geometric']).flatten()  # geometric
# #                             connected_2 = torch.nonzero(torch.logical_and(relations_target[graph_iter - 1][edge_iter][connected] >= args['models']['num_geometric'],
# #                                                                           relations_target[graph_iter - 1][edge_iter][connected] < args['models']['num_geometric']+args['models']['num_possessive'])).flatten()  # possessive
# #                             connected_3 = torch.nonzero(relations_target[graph_iter - 1][edge_iter][connected] >= args['models']['num_geometric']+args['models']['num_possessive']).flatten()  # semantic
# #                             if len(connected_1) > 0:
# #                                 loss_relationship += criterion_relationship_1(relation_1[connected][connected_1], relations_target[graph_iter - 1][edge_iter][connected][connected_1])
# #                             if len(connected_2) > 0:
# #                                 loss_relationship += criterion_relationship_2(relation_2[connected][connected_2], relations_target[graph_iter - 1][edge_iter][connected][connected_2] - args['models']['num_geometric'])
# #                             if len(connected_3) > 0:
# #                                 loss_relationship += criterion_relationship_3(relation_3[connected][connected_3], relations_target[graph_iter - 1][edge_iter][connected][connected_3] - args['models']['num_geometric'] - args['models']['num_possessive'])
# #                         else:
# #                             loss_relationship += criterion_relationship(relation[connected], relations_target[graph_iter - 1][edge_iter][connected])
# #
# #                         hidden_cat_labels2 = relations_target[graph_iter - 1][edge_iter][connected]
# #                         for index, batch_index in enumerate(keep_in_batch[connected]):
# #                             hidden_cat_accumulated[batch_index].append(hidden_cat2[index])
# #                             hidden_cat_labels_accumulated[batch_index].append(hidden_cat_labels2[index])
# #
# #                     # evaluate recall@k scores
# #                     relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
# #                     relations_target_directed[not_connected] = -1
# #
# #                     if (batch_count % args['training']['eval_freq'] == 0) or (batch_count + 1 == len(train_loader)):
# #                         Recall.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
# #                                           cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
# #                         if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
# #                             Recall_top3.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
# #                                                    cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
# #
# #                     losses += loss_relationship + args['training']['lambda_connectivity'] * (
# #                                 loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1))
# #                     running_loss_connectivity += args['training']['lambda_connectivity'] * (
# #                                 loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1))
# #                     running_loss_relationship += loss_relationship
# #
# #             if not all(len(sublist) == 0 for sublist in hidden_cat_accumulated):
# #                 # concatenate all hidden_cat and hidden_cat_labels along the 0th dimension
# #                 hidden_cat_accumulated = [torch.stack(sublist) for sublist in hidden_cat_accumulated if len(sublist) > 0]
# #                 hidden_cat_labels_accumulated = [torch.stack(sublist) for sublist in hidden_cat_labels_accumulated if len(sublist) > 0]
# #
# #                 hidden_cat_all = torch.cat(hidden_cat_accumulated, dim=0)
# #                 hidden_cat_labels_all = torch.cat(hidden_cat_labels_accumulated, dim=0)
# #
# #                 temp = criterion_contrast(rank, hidden_cat_all, hidden_cat_labels_all)
# #                 loss_contrast += 0.0 if torch.isnan(temp) else args['training']['lambda_contrast'] * temp
# #
# #             running_loss_contrast += args['training']['lambda_contrast'] * loss_contrast
# #             losses += args['training']['lambda_contrast'] * loss_contrast
# #             running_losses += losses.item()
# #
# #             optimizer.zero_grad()
# #             losses.backward()
# #             optimizer.step()
# #             # if (batch_count + 1) % accumulation_steps == 0:     # gradient accumulation
# #             #     optimizer.step()
# #             #     optimizer.zero_grad()
# #
# #             if rank == 0:
# #                 global_step = batch_count + len(train_loader) * epoch
# #                 writer.add_scalar('train/running_loss_relationship', running_loss_relationship, global_step)
# #                 writer.add_scalar('train/running_loss_connectivity', running_loss_connectivity, global_step)
# #                 writer.add_scalar('train/running_loss_contrast', running_loss_contrast, global_step)
# #                 writer.add_scalar('train/running_losses', running_losses, global_step)
# #
# #             """
# #             EVALUATE AND PRINT CURRENT TRAINING RESULTS
# #             """
# #             if (batch_count % args['training']['eval_freq'] == 0) or (batch_count + 1 == len(train_loader)):
# #                 recall_top3, mean_recall_top3 = None, None
# #                 if args['dataset']['dataset'] == 'vg':
# #                     recall, _, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True)
# #                     if rank == 0:
# #                         writer.add_scalar('train/Recall@20', recall[0], global_step)
# #                         writer.add_scalar('train/Recall@50', recall[1], global_step)
# #                         writer.add_scalar('train/Recall@100', recall[2], global_step)
# #                     if args['models']['hierarchical_pred']:
# #                         recall_top3, _, mean_recall_top3 = Recall_top3.compute(per_class=True)
# #                         Recall_top3.clear_data()
# #                 else:
# #                     recall, _, mean_recall, _, _, _ = Recall.compute(per_class=True)
# #                     wmap_rel, wmap_phrase = Recall.compute_precision()
# #                 Recall.clear_data()
# #
# #             if (batch_count % args['training']['print_freq'] == 0) or (batch_count + 1 == len(train_loader)):
# #                 record_train_results(args, record, rank, epoch, batch_count, optimizer.param_groups[0]['lr'], recall_top3, recall, mean_recall_top3, mean_recall,
# #                                      recall_zs, mean_recall_zs, running_losses, running_loss_relationship, running_loss_contrast, running_loss_connectivity,
# #                                      connectivity_recall, num_connected, num_not_connected, connectivity_precision, num_connected_pred, wmap_rel, wmap_phrase)
# #                 dist.monitored_barrier()
# #
# #             running_losses, running_loss_connectivity, running_loss_relationship, running_loss_contrast, connectivity_precision, \
# #                 num_connected, num_not_connected = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
# #
# #         if rank == 0:
# #             if args['models']['hierarchical_pred']:
# #                 torch.save(local_predictor.state_dict(), args['training']['checkpoint_path'] + 'HierMotif_4' + str(epoch) + '.pth')
# #             else:
# #                 torch.save(local_predictor.state_dict(), args['training']['checkpoint_path'] + 'FlatMotif_4' + str(epoch) + '.pth')
# #         dist.monitored_barrier()
# #
# #         test_local(args, detr, local_predictor, test_loader, test_record, epoch, rank)
# #
# #     dist.destroy_process_group()  # clean up
# #     if rank == 0:
# #         writer.close()
# #     print('FINISHED TRAINING\n')
# #
# #
# # def test_local(args, backbone, local_predictor, test_loader, test_record, epoch, rank):
# #     backbone.eval()
# #     local_predictor.eval()
# #
# #     connectivity_recall, connectivity_precision, num_connected, num_not_connected, num_connected_pred = 0.0, 0.0, 0.0, 0.0, 0.0
# #     recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs, wmap_rel, wmap_phrase = None, None, None, None, None, None, None
# #     Recall = Evaluator_PC(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
# #     if args['dataset']['dataset'] == 'vg':
# #         Recall_top3 = Evaluator_PC_Top3(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
# #
# #     print('Start Testing PC...')
# #     with torch.no_grad():
# #         for batch_count, data in enumerate(tqdm(test_loader), 0):
# #             """
# #             PREPARE INPUT DATA
# #             """
# #             try:
# #                 images, _, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, _ = data
# #             except:
# #                 continue
# #
# #             images = torch.stack(images).to(rank)
# #             image_feature, pos_embed = backbone.module.backbone(nested_tensor_from_tensor_list(images))
# #             src, mask = image_feature[-1].decompose()
# #             src = backbone.module.input_proj(src).flatten(2).permute(2, 0, 1)
# #             pos_embed = pos_embed[-1].flatten(2).permute(2, 0, 1)
# #             image_feature = backbone.module.transformer.encoder(src, src_key_padding_mask=mask.flatten(1), pos=pos_embed)
# #             image_feature = image_feature.permute(1, 2, 0)
# #             image_feature = image_feature.view(-1, args['models']['num_img_feature'], args['models']['feature_size'], args['models']['feature_size'])
# #             del images
# #
# #             categories = [category.to(rank) for category in categories]  # [batch_size][curr_num_obj, 1]
# #             if super_categories[0] is not None:
# #                 super_categories = [[sc.to(rank) for sc in super_category] for super_category in super_categories]  # [batch_size][curr_num_obj, [1 or more]]
# #             image_depth = torch.stack([depth.to(rank) for depth in image_depth])
# #             bbox = [box.to(rank) for box in bbox]  # [batch_size][curr_num_obj, 4]
# #
# #             masks = []
# #             for i in range(len(bbox)):
# #                 mask = torch.zeros(bbox[i].shape[0], args['models']['feature_size'], args['models']['feature_size'], dtype=torch.bool).to(rank)
# #                 for j, box in enumerate(bbox[i]):
# #                     mask[j, int(bbox[i][j][2]):int(bbox[i][j][3]), int(bbox[i][j][0]):int(bbox[i][j][1])] = 1
# #                 masks.append(mask)
# #
# #             """
# #             PREPARE TARGETS
# #             """
# #             relations_target = []
# #             direction_target = []
# #             num_graph_iter = torch.as_tensor([len(mask) for mask in masks]) - 1
# #             for graph_iter in range(max(num_graph_iter)):
# #                 keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)
# #                 relations_target.append(torch.vstack([relationships[i][graph_iter] for i in keep_in_batch]).T.to(rank))  # integer labels
# #                 direction_target.append(torch.vstack([subj_or_obj[i][graph_iter] for i in keep_in_batch]).T.to(rank))
# #
# #             """
# #             FORWARD PASS THROUGH THE LOCAL PREDICTOR
# #             """
# #             num_graph_iter = torch.as_tensor([len(mask) for mask in masks])
# #             for graph_iter in range(max(num_graph_iter)):
# #                 keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1).to(rank)
# #
# #                 curr_graph_masks = torch.stack([torch.unsqueeze(masks[i][graph_iter], dim=0) for i in keep_in_batch])
# #                 h_graph = torch.cat((image_feature[keep_in_batch] * curr_graph_masks, image_depth[keep_in_batch] * curr_graph_masks), dim=1)  # (bs, 256, 64, 64), (bs, 1, 64, 64)
# #                 cat_graph = torch.tensor([torch.unsqueeze(categories[i][graph_iter], dim=0) for i in keep_in_batch]).to(rank)
# #                 scat_graph = [super_categories[i][graph_iter] for i in keep_in_batch] if super_categories[0] is not None else None
# #                 bbox_graph = torch.stack([bbox[i][graph_iter] for i in keep_in_batch]).to(rank)
# #
# #                 for edge_iter in range(graph_iter):
# #                     curr_edge_masks = torch.stack([torch.unsqueeze(masks[i][edge_iter], dim=0) for i in keep_in_batch])  # seg mask of every prev obj
# #                     h_edge = torch.cat((image_feature[keep_in_batch] * curr_edge_masks, image_depth[keep_in_batch] * curr_edge_masks), dim=1)
# #                     cat_edge = torch.tensor([torch.unsqueeze(categories[i][edge_iter], dim=0) for i in keep_in_batch]).to(rank)
# #                     scat_edge = [super_categories[i][edge_iter] for i in keep_in_batch] if super_categories[0] is not None else None
# #                     bbox_edge = torch.stack([bbox[i][edge_iter] for i in keep_in_batch]).to(rank)
# #
# #                     """
# #                     FIRST DIRECTION
# #                     """
# #                     if args['models']['hierarchical_pred']:
# #                         relation_1, relation_2, relation_3, super_relation, connectivity, _, _ = local_predictor(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge, rank)
# #                         relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
# #                     else:
# #                         relation, connectivity, _, _ = local_predictor(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge, rank)
# #                         super_relation = None
# #
# #                     not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 1)[0]  # which data samples in curr keep_in_batch are not connected
# #                     num_not_connected += len(not_connected)
# #                     connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 1)[0]  # which data samples in curr keep_in_batch are connected
# #                     num_connected += len(connected)
# #                     connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
# #                     connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
# #                     num_connected_pred += len(connected_pred)
# #
# #                     if len(connected) > 0:
# #                         connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))
# #
# #                     # evaluate recall@k scores
# #                     relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
# #                     relations_target_directed[not_connected] = -1
# #
# #                     if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
# #                         Recall.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
# #                                           cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
# #                         if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
# #                             Recall_top3.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
# #                                                    cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
# #
# #                     """
# #                     SECOND DIRECTION
# #                     """
# #                     if args['models']['hierarchical_pred']:
# #                         relation_1, relation_2, relation_3, super_relation, connectivity, _, _ = local_predictor(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph, rank)
# #                         relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
# #                     else:
# #                         relation, connectivity, _, _ = local_predictor(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph, rank)
# #                         super_relation = None
# #
# #                     not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 0)[0]  # which data samples in curr keep_in_batch are not connected
# #                     num_not_connected += len(not_connected)
# #                     connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 0)[0]  # which data samples in curr keep_in_batch are connected
# #                     num_connected += len(connected)
# #                     connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
# #                     connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
# #                     num_connected_pred += len(connected_pred)
# #
# #                     if len(connected) > 0:
# #                         connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))
# #
# #                     relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
# #                     relations_target_directed[not_connected] = -1
# #
# #                     if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
# #                         Recall.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
# #                                           cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
# #                         if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
# #                             Recall_top3.accumulate(keep_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
# #                                                    cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
# #
# #             """
# #             EVALUATE AND PRINT CURRENT RESULTS
# #             """
# #             if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
# #                 recall_top3, mean_recall_top3 = None, None
# #                 if args['dataset']['dataset'] == 'vg':
# #                     recall, _, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True)
# #                     if args['models']['hierarchical_pred']:
# #                         recall_top3, _, mean_recall_top3 = Recall_top3.compute(per_class=True)
# #                         Recall_top3.clear_data()
# #                 else:
# #                     recall, _, mean_recall, _, _, _ = Recall.compute(per_class=True)
# #                     wmap_rel, wmap_phrase = Recall.compute_precision()
# #                 Recall.clear_data()
# #
# #             if (batch_count % args['training']['print_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
# #                 record_test_results(args, test_record, rank, epoch, recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs,
# #                                     connectivity_recall, num_connected, num_not_connected, connectivity_precision, num_connected_pred, wmap_rel, wmap_phrase)
# #                 dist.monitored_barrier()
# #     print('FINISHED EVALUATING\n')