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

from evaluator import Evaluator_PC, Evaluator_PC_Top3
from model import EdgeHead, EdgeHeadHier
from utils import *

# from detectron2.modeling import build_model
# from detectron2.structures.image_list import ImageList


def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12356'
    dist.init_process_group("gloo", rank=rank, world_size=world_size)


def train_local(gpu, args, train_subset, test_subset, faster_rcnn_cfg=None):
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
        edge_head = DDP(EdgeHeadHier(args=args, input_dim=args['models']['hidden_dim'], feature_size=args['models']['feature_size'],
                                     num_classes=args['models']['num_classes'], num_super_classes=args['models']['num_super_classes'],
                                     num_geometric=args['models']['num_geometric'], num_possessive=args['models']['num_possessive'], num_semantic=args['models']['num_semantic'])).to(rank)
    else:
        edge_head = DDP(EdgeHead(args=args, input_dim=args['models']['hidden_dim'], output_dim=args['models']['num_relations'], feature_size=args['models']['feature_size'],
                                 num_classes=args['models']['num_classes'], num_super_classes=args['models']['num_super_classes'],
                                 num_geometric=args['models']['num_geometric'], num_possessive=args['models']['num_possessive'], num_semantic=args['models']['num_semantic'])).to(rank)

    if args['models']['detr_or_faster_rcnn'] == 'detr':
        detr = DDP(build_detr101(args)).to(rank)
        edge_head.eval()
    elif args['models']['detr_or_faster_rcnn'] == 'faster':
        faster_rcnn = build_model(faster_rcnn_cfg).to(rank)
        faster_rcnn.load_state_dict(torch.load(os.path.join(faster_rcnn_cfg.OUTPUT_DIR, "model_final.pth"))['model'], strict=True)
        faster_rcnn = DDP(faster_rcnn)
        faster_rcnn.eval()
    else:
        print('Unknown model.')

    map_location = {'cuda:%d' % rank: 'cuda:%d' % 0}
    if args['training']['continue_train']:
        if args['models']['hierarchical_pred']:
            edge_head.load_state_dict(torch.load(args['training']['checkpoint_path'] + 'EdgeHeadHier' + str(args['training']['start_epoch'] - 1) + '_0' + '.pth', map_location=map_location))
        else:
            edge_head.load_state_dict(torch.load(args['training']['checkpoint_path'] + 'EdgeHead' + str(args['training']['start_epoch'] - 1) + '_0' + '.pth', map_location=map_location))

    optimizer = optim.SGD([{'params': edge_head.parameters(), 'initial_lr': args['training']['learning_rate']}],
                          lr=args['training']['learning_rate'], momentum=0.9, weight_decay=args['training']['weight_decay'])
    original_lr = optimizer.param_groups[0]["lr"]

    relation_count = get_num_each_class_reordered(args)
    class_weight = 1 - relation_count / torch.sum(relation_count)
    criterion_relationship_1 = torch.nn.NLLLoss(weight=class_weight[:args['models']['num_geometric']].to(rank))  # log softmax already applied
    criterion_relationship_2 = torch.nn.NLLLoss(weight=class_weight[args['models']['num_geometric']:args['models']['num_geometric']+args['models']['num_possessive']].to(rank))
    criterion_relationship_3 = torch.nn.NLLLoss(weight=class_weight[args['models']['num_geometric']+args['models']['num_possessive']:].to(rank))
    criterion_super_relationship = torch.nn.NLLLoss()
    criterion_relationship = torch.nn.CrossEntropyLoss(weight=class_weight.to(rank))
    criterion_connectivity = torch.nn.BCEWithLogitsLoss()  # pos_weight=torch.tensor([20]).to(rank)

    running_losses, running_loss_connectivity, running_loss_relationship, connectivity_recall, connectivity_precision, \
    num_connected, num_not_connected, num_connected_pred = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs, wmap_rel, wmap_phrase = None, None, None, None, None, None, None, None

    Recall = Evaluator_PC(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
    if args['dataset']['dataset'] == 'vg':
        Recall_top3 = Evaluator_PC_Top3(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])

    lr_decay = 1
    for epoch in range(args['training']['start_epoch'], args['training']['num_epoch']):
        print('Start Training... EPOCH %d / %d\n' % (epoch, args['training']['num_epoch']))
        if epoch == args['training']['scheduler_param1'] or epoch == args['training']['scheduler_param2']:  # lr scheduler
            lr_decay *= 0.1

        for batch_count, data in enumerate(tqdm(train_loader), 0):
            """
            PREPARE INPUT DATA
            """
            # _, images, image_depth, categories, super_categories, masks, bbox, relationships, subj_or_obj = data
            images, image_depth, categories, super_categories, bbox, relationships, subj_or_obj = data

            with torch.no_grad():
                if args['models']['detr_or_faster_rcnn'] == 'detr':
                    images = torch.stack(images).to(rank)
                    image_feature, pos_embed = detr.module.backbone(nested_tensor_from_tensor_list(images))
                    src, mask = image_feature[-1].decompose()
                    src = detr.module.input_proj(src).flatten(2).permute(2, 0, 1)
                    pos_embed = pos_embed[-1].flatten(2).permute(2, 0, 1)
                    image_feature = detr.module.transformer.encoder(src, src_key_padding_mask=mask.flatten(1), pos=pos_embed)
                    image_feature = image_feature.permute(1, 2, 0)
                    image_feature = image_feature.view(-1, args['models']['num_img_feature'], args['models']['feature_size'], args['models']['feature_size'])
                else:   # faster-rcnn
                    images = ImageList.from_tensors(images).to(rank)
                    image_feature = faster_rcnn.module.backbone(images.tensor)['p5']
                del images

            categories = [category.to(rank) for category in categories]
            if super_categories[0] is not None:
                super_categories = [[sc.to(rank) for sc in super_category] for super_category in super_categories]  # [batch_size][curr_num_obj, [1 or more]]
            image_depth = torch.stack([depth for depth in image_depth]).to(rank)
            bbox = [box.to(rank) for box in bbox]
            optimizer.param_groups[0]["lr"] = original_lr

            masks = []
            for i in range(len(bbox)):
                mask = torch.zeros(bbox[i].shape[0], args['models']['feature_size'], args['models']['feature_size'], dtype=torch.uint8).to(rank)
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
                which_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)
                relations_target.append(torch.vstack([relationships[i][graph_iter] for i in which_in_batch]).T.to(rank))  # integer labels
                direction_target.append(torch.vstack([subj_or_obj[i][graph_iter] for i in which_in_batch]).T.to(rank))

            """
            FORWARD PASS THROUGH THE LOCAL PREDICTOR
            """
            num_graph_iter = torch.as_tensor([len(mask) for mask in masks]).to(rank)
            for graph_iter in range(max(num_graph_iter)):

                for edge_iter in range(graph_iter):
                    has_relations_mask = relations_target[graph_iter - 1][edge_iter] != -1
                    which_in_batch = num_graph_iter > graph_iter
                    which_in_batch[which_in_batch == True] = torch.logical_and(which_in_batch[which_in_batch == True], has_relations_mask)
                    which_in_batch = torch.nonzero(which_in_batch).view(-1)

                    if len(which_in_batch) == 0:
                        continue
                    # optimizer.param_groups[0]["lr"] = original_lr * lr_decay * math.sqrt(len(which_in_batch) / len(num_graph_iter))

                    curr_direction_target = direction_target[graph_iter - 1][edge_iter][has_relations_mask]

                    # determine the indices for subject and object based on curr_direction_target
                    subject_iters = [graph_iter if direction == 1 else edge_iter for direction in curr_direction_target]
                    object_iters = [edge_iter if direction == 1 else graph_iter for direction in curr_direction_target]

                    curr_subject_masks = torch.stack([torch.unsqueeze(masks[i][subject_iters[j]], dim=0) for j, i in enumerate(which_in_batch)])
                    h_subject = torch.cat((image_feature[which_in_batch] * curr_subject_masks, image_depth[which_in_batch] * curr_subject_masks), dim=1)
                    cat_subject = torch.tensor([torch.unsqueeze(categories[i][subject_iters[j]], dim=0) for j, i in enumerate(which_in_batch)]).to(rank)
                    scat_subject = [super_categories[i][subject_iters[j]] for j, i in enumerate(which_in_batch)] if super_categories[0] is not None else None
                    bbox_subject = torch.stack([bbox[i][subject_iters[j]] for j, i in enumerate(which_in_batch)]).to(rank)

                    curr_object_masks = torch.stack([torch.unsqueeze(masks[i][object_iters[j]], dim=0) for j, i in enumerate(which_in_batch)])
                    h_object = torch.cat((image_feature[which_in_batch] * curr_object_masks, image_depth[which_in_batch] * curr_object_masks), dim=1)
                    cat_object = torch.tensor([torch.unsqueeze(categories[i][object_iters[j]], dim=0) for j, i in enumerate(which_in_batch)]).to(rank)
                    scat_object = [super_categories[i][object_iters[j]] for j, i in enumerate(which_in_batch)] if super_categories[0] is not None else None
                    bbox_object = torch.stack([bbox[i][object_iters[j]] for j, i in enumerate(which_in_batch)]).to(rank)

                    curr_relations_target = relations_target[graph_iter - 1][edge_iter][has_relations_mask]

                    loss_connectivity, loss_relationship = 0.0, 0.0
                    if args['models']['hierarchical_pred']:
                        relation_1, relation_2, relation_3, super_relation, connectivity = edge_head(h_subject, h_object, cat_subject, cat_object, scat_subject, scat_object, rank)
                        relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
                    else:
                        relation, connectivity = edge_head(h_subject, h_object, cat_subject, cat_object, scat_subject, scat_object, rank)
                        super_relation = None

                    if args['models']['hierarchical_pred']:
                        super_relation_target = curr_relations_target.clone()
                        super_relation_target[super_relation_target < args['models']['num_geometric']] = 0
                        super_relation_target[torch.logical_and(super_relation_target >= args['models']['num_geometric'],
                                                                super_relation_target < args['models']['num_geometric']+args['models']['num_possessive'])] = 1
                        super_relation_target[super_relation_target >= args['models']['num_geometric']+args['models']['num_possessive']] = 2
                        loss_relationship += criterion_super_relationship(super_relation, super_relation_target)

                        connected_1 = torch.nonzero(curr_relations_target < args['models']['num_geometric']).flatten()  # geometric
                        connected_2 = torch.nonzero(torch.logical_and(curr_relations_target >= args['models']['num_geometric'],
                                                                      curr_relations_target < args['models']['num_geometric']+args['models']['num_possessive'])).flatten()  # possessive
                        connected_3 = torch.nonzero(curr_relations_target >= args['models']['num_geometric'] + args['models']['num_possessive']).flatten()  # semantic

                        if len(connected_1) > 0:
                            loss_relationship += criterion_relationship_1(relation_1[connected_1], curr_relations_target[connected_1])
                        if len(connected_2) > 0:
                            loss_relationship += criterion_relationship_2(relation_2[connected_2], curr_relations_target[connected_2] - args['models']['num_geometric'])
                        if len(connected_3) > 0:
                            loss_relationship += criterion_relationship_3(relation_3[connected_3], curr_relations_target[connected_3]
                                                                          - args['models']['num_geometric'] - args['models']['num_possessive'])
                    else:
                        loss_relationship += criterion_relationship(relation, curr_relations_target)

                    losses = loss_relationship  # + args['training']['lambda_connectivity'] * (
                    # loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1))
                    # running_loss_connectivity += args['training']['lambda_connectivity'] * (
                    #             loss_connectivity + args['training']['lambda_sparsity'] * torch.linalg.norm(torch.sigmoid(connectivity), ord=1))
                    running_loss_relationship += loss_relationship
                    running_losses += losses.item()

                    optimizer.zero_grad()
                    losses.backward()
                    optimizer.step()

                    if (batch_count % args['training']['eval_freq'] == 0) or (batch_count + 1 == len(train_loader)):
                        relation = torch.softmax(relation, dim=1)
                        Recall.accumulate(which_in_batch, relation, curr_relations_target, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                      cat_subject, cat_object, cat_subject, cat_object, bbox_subject, bbox_object, bbox_subject, bbox_object)
                        if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
                            Recall_top3.accumulate(which_in_batch, relation, curr_relations_target, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                                   cat_subject, cat_object, cat_subject, cat_object, bbox_subject, bbox_object, bbox_subject, bbox_object)

            """
            EVALUATE AND PRINT CURRENT TRAINING RESULTS
            """
            if (batch_count % args['training']['eval_freq'] == 0) or (batch_count + 1 == len(train_loader)):
                if args['dataset']['dataset'] == 'vg':
                    if batch_count + 1 == len(train_loader) and rank == 0:
                        recall, recall_k_per_class, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True)
                        print("recall_k_per_class", recall_k_per_class)
                    recall, _, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True)
                    if args['models']['hierarchical_pred']:
                        recall_top3, _, mean_recall_top3 = Recall_top3.compute(per_class=True)
                        Recall_top3.clear_data()
                else:
                    recall, _, mean_recall, _, _, _ = Recall.compute(per_class=True)
                    wmap_rel, wmap_phrase = Recall.compute_precision()
                Recall.clear_data()

            if (batch_count % args['training']['print_freq'] == 0) or (batch_count + 1 == len(train_loader)):
                record_train_results(args, record, rank, epoch, batch_count, original_lr, lr_decay, recall_top3, recall, mean_recall_top3, mean_recall,
                                     recall_zs, mean_recall_zs, running_losses, running_loss_relationship, running_loss_connectivity, connectivity_recall,
                                     num_connected, num_not_connected, connectivity_precision, num_connected_pred, wmap_rel, wmap_phrase)
                dist.monitored_barrier()

            running_losses, running_loss_connectivity, running_loss_relationship, connectivity_recall, connectivity_precision, num_connected, num_not_connected = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

        if rank == 0:
            if args['models']['hierarchical_pred']:
                torch.save(edge_head.state_dict(), args['training']['checkpoint_path'] + 'EdgeHeadHier' + str(epoch) + '_' + str(rank) + '.pth')
            else:
                torch.save(edge_head.state_dict(), args['training']['checkpoint_path'] + 'EdgeHead' + str(epoch) + '_' + str(rank) + '.pth')
        dist.monitored_barrier()

        if args['models']['detr_or_faster_rcnn'] == 'detr':
            test_local(args, detr, edge_head, test_loader, test_record, epoch, rank)
        else:
            test_local(args, faster_rcnn, edge_head, test_loader, test_record, epoch, rank)

    dist.destroy_process_group()  # clean up
    print('FINISHED TRAINING\n')


def test_local(args, backbone, edge_head, test_loader, test_record, epoch, rank):
    backbone.eval()

    connectivity_recall, connectivity_precision, num_connected, num_not_connected, num_connected_pred = 0.0, 0.0, 0.0, 0.0, 0.0
    recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs, wmap_rel, wmap_phrase = None, None, None, None, None, None, None, None
    Recall = Evaluator_PC(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
    if args['dataset']['dataset'] == 'vg':
        Recall_top3 = Evaluator_PC_Top3(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])

    print('Start Testing PC...')
    with torch.no_grad():
        for batch_count, data in enumerate(tqdm(test_loader), 0):
            """
            PREPARE INPUT DATA
            """
            # _, images, image_depth, categories, super_categories, masks, bbox, relationships, subj_or_obj = data
            images, image_depth, categories, super_categories, bbox, relationships, subj_or_obj = data

            if args['models']['detr_or_faster_rcnn'] == 'detr':
                images = torch.stack(images).to(rank)
                image_feature, pos_embed = backbone.module.backbone(nested_tensor_from_tensor_list(images))
                src, mask = image_feature[-1].decompose()
                src = backbone.module.input_proj(src).flatten(2).permute(2, 0, 1)
                pos_embed = pos_embed[-1].flatten(2).permute(2, 0, 1)
                image_feature = backbone.module.transformer.encoder(src, src_key_padding_mask=mask.flatten(1), pos=pos_embed)
                image_feature = image_feature.permute(1, 2, 0)
                image_feature = image_feature.view(-1, args['models']['num_img_feature'], args['models']['feature_size'], args['models']['feature_size'])
            else:  # faster-rcnn
                images = ImageList.from_tensors(images).to(rank)
                image_feature = backbone.module.backbone(images.tensor)['p5']
            del images

            categories = [category.to(rank) for category in categories]  # [batch_size][curr_num_obj, 1]
            if super_categories[0] is not None:
                super_categories = [[sc.to(rank) for sc in super_category] for super_category in super_categories]  # [batch_size][curr_num_obj, [1 or more]]
            image_depth = torch.stack([depth.to(rank) for depth in image_depth])
            bbox = [box.to(rank) for box in bbox]  # [batch_size][curr_num_obj, 4]

            masks = []
            for i in range(len(bbox)):
                mask = torch.zeros(bbox[i].shape[0], args['models']['feature_size'], args['models']['feature_size'], dtype=torch.uint8).to(rank)
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
                which_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)
                relations_target.append(torch.vstack([relationships[i][graph_iter] for i in which_in_batch]).T.to(rank))  # integer labels
                direction_target.append(torch.vstack([subj_or_obj[i][graph_iter] for i in which_in_batch]).T.to(rank))

            """
            FORWARD PASS THROUGH THE LOCAL PREDICTOR
            """
            num_graph_iter = torch.as_tensor([len(mask) for mask in masks])
            for graph_iter in range(max(num_graph_iter)):
                which_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)

                curr_graph_masks = torch.stack([torch.unsqueeze(masks[i][graph_iter], dim=0) for i in which_in_batch])
                h_graph = torch.cat((image_feature[which_in_batch] * curr_graph_masks, image_depth[which_in_batch] * curr_graph_masks), dim=1)  # (bs, 256, 64, 64), (bs, 1, 64, 64)
                cat_graph = torch.tensor([torch.unsqueeze(categories[i][graph_iter], dim=0) for i in which_in_batch]).to(rank)
                scat_graph = [super_categories[i][graph_iter] for i in which_in_batch] if super_categories[0] is not None else None
                bbox_graph = torch.stack([bbox[i][graph_iter] for i in which_in_batch]).to(rank)

                for edge_iter in range(graph_iter):
                    curr_edge_masks = torch.stack([torch.unsqueeze(masks[i][edge_iter], dim=0) for i in which_in_batch])  # seg mask of every prev obj
                    h_edge = torch.cat((image_feature[which_in_batch] * curr_edge_masks, image_depth[which_in_batch] * curr_edge_masks), dim=1)
                    cat_edge = torch.tensor([torch.unsqueeze(categories[i][edge_iter], dim=0) for i in which_in_batch]).to(rank)
                    scat_edge = [super_categories[i][edge_iter] for i in which_in_batch] if super_categories[0] is not None else None
                    bbox_edge = torch.stack([bbox[i][edge_iter] for i in which_in_batch]).to(rank)

                    """
                    FIRST DIRECTION
                    """
                    if args['models']['hierarchical_pred']:
                        relation_1, relation_2, relation_3, super_relation, connectivity = edge_head(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge, rank)
                        relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
                    else:
                        relation, connectivity = edge_head(h_graph, h_edge, cat_graph, cat_edge, scat_graph, scat_edge, rank)
                        super_relation = None

                    not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 1)[0]  # which data samples in curr which_in_batch are not connected
                    # num_not_connected += len(not_connected)
                    connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 1)[0]  # which data samples in curr which_in_batch are connected
                    # num_connected += len(connected)
                    # connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
                    # connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
                    # num_connected_pred += len(connected_pred)
                    # if len(connected) > 0:
                    #     connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))

                    # evaluate recall@k scores
                    relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
                    relations_target_directed[not_connected] = -1

                    if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                        Recall.accumulate(which_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                          cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
                        if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
                            Recall_top3.accumulate(which_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                                   cat_graph, cat_edge, cat_graph, cat_edge, bbox_graph, bbox_edge, bbox_graph, bbox_edge)

                    """
                    SECOND DIRECTION
                    """
                    if args['models']['hierarchical_pred']:
                        relation_1, relation_2, relation_3, super_relation, connectivity = edge_head(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph, rank)
                        relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
                    else:
                        relation, connectivity = edge_head(h_edge, h_graph, cat_edge, cat_graph, scat_edge, scat_graph, rank)
                        super_relation = None

                    not_connected = torch.where(direction_target[graph_iter - 1][edge_iter] != 0)[0]  # which data samples in curr which_in_batch are not connected
                    # num_not_connected += len(not_connected)
                    connected = torch.where(direction_target[graph_iter - 1][edge_iter] == 0)[0]  # which data samples in curr which_in_batch are connected
                    # num_connected += len(connected)
                    # connected_pred = torch.nonzero(torch.sigmoid(connectivity[:, 0]) >= 0.5).flatten()
                    # connectivity_precision += torch.sum(relations_target[graph_iter - 1][edge_iter][connected_pred] != -1)
                    # num_connected_pred += len(connected_pred)
                    # if len(connected) > 0:
                    #     connectivity_recall += torch.sum(torch.round(torch.sigmoid(connectivity[connected, 0])))

                    relations_target_directed = relations_target[graph_iter - 1][edge_iter].clone()
                    relations_target_directed[not_connected] = -1

                    if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                        relation = torch.softmax(relation, dim=1)
                        Recall.accumulate(which_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                          cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge)
                        if args['dataset']['dataset'] == 'vg' and args['models']['hierarchical_pred']:
                            Recall_top3.accumulate(which_in_batch, relation, relations_target_directed, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                                   cat_edge, cat_graph, cat_edge, cat_graph, bbox_graph, bbox_edge, bbox_graph, bbox_edge)

            """
            EVALUATE AND PRINT CURRENT RESULTS
            """
            if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                if args['dataset']['dataset'] == 'vg':
                    if batch_count + 1 == len(test_loader) and rank == 0:
                        recall, recall_k_per_class, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True)
                        print("recall_k_per_class", recall_k_per_class)
                    else:
                        recall, _, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True)
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
    print('FINISHED EVALUATING\n')
