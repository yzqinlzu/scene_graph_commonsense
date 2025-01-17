import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
from tqdm import tqdm
import json
import yaml
import os
import math
import torchvision
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
import datetime

from evaluator import *
from model import *
from utils import *
from train_utils import *
from dataset_utils import object_class_alp2fre


def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'
    dist.init_process_group("gloo", rank=rank, world_size=world_size)


def eval_pc(gpu, args, test_subset, curr_dataset=None, prepare_cs_step=-1):
    """
    This function evaluates the module on predicate classification tasks.
    :param gpu: current gpu index
    :param args: input arguments in config.yaml
    :param test_subset: testing dataset
    """
    rank = gpu
    world_size = torch.cuda.device_count()
    setup(rank, world_size)
    print('rank', rank, 'torch.distributed.is_initialized', torch.distributed.is_initialized())

    if args['training']['run_mode'] == 'prepare_cs':
        curr_dataset.train_cs_step = prepare_cs_step

    test_sampler = torch.utils.data.distributed.DistributedSampler(test_subset, num_replicas=world_size, rank=rank)
    test_loader = torch.utils.data.DataLoader(test_subset, batch_size=args['training']['batch_size'], shuffle=False, collate_fn=collate_fn, num_workers=0, drop_last=True, sampler=test_sampler)
    print("Finished loading the datasets...")

    start = []
    test_record = []
    with open(args['training']['result_path'] + 'test_results_' + str(rank) + '.json', 'w') as f:  # clear history logs
        json.dump(start, f)

    if args['models']['hierarchical_pred']:
        relation_classifier = DDP(BayesianRelationClassifier(args=args, input_dim=args['models']['hidden_dim'], feature_size=args['models']['feature_size'],
                                                             num_classes=args['models']['num_classes'], num_super_classes=args['models']['num_super_classes'],
                                                             num_geometric=args['models']['num_geometric'], num_possessive=args['models']['num_possessive'],
                                                             num_semantic=args['models']['num_semantic'])).to(rank)
    else:
        relation_classifier = DDP(FlatRelationClassifier(args=args, input_dim=args['models']['hidden_dim'], output_dim=args['models']['num_relations'],
                                                         feature_size=args['models']['feature_size'], num_classes=args['models']['num_classes'])).to(rank)

    detr = DDP(build_detr101(args)).to(rank)
    relation_classifier.eval()

    map_location = {'cuda:%d' % rank: 'cuda:%d' % 0}
    if args['models']['hierarchical_pred']:
        load_model_name = 'HierRelationModel_CS' if args['training']['run_mode'] == 'prepare_cs' or args['training']['run_mode'] == 'eval_cs' else 'HierRelationModel_Baseline'
        load_model_name = args['training']['checkpoint_path'] + load_model_name + str(args['training']['test_epoch']) + '_0' + '.pth'
    else:
        load_model_name = 'FlatRelationModel_CS' if args['training']['run_mode'] == 'prepare_cs' or args['training']['run_mode'] == 'eval_cs' else 'FlatRelationModel_Baseline'
        load_model_name = args['training']['checkpoint_path'] + load_model_name + str(args['training']['test_epoch']) + '_0' + '.pth'
    if rank == 0:
        print('Loading pretrained model from %s...' % load_model_name)
    relation_classifier.load_state_dict(torch.load(load_model_name, map_location=map_location))

    recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs, wmap_rel, wmap_phrase = None, None, None, None, None, None, None
    Recall = Evaluator(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
    Recall_top3 = None
    if args['dataset']['dataset'] == 'vg':
        Recall_top3 = Evaluator_Top3(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])
    connectivity_recall, connectivity_precision, num_connected, num_not_connected, num_connected_pred = 0.0, 0.0, 0.0, 0.0, 0.0

    print('Start Testing PC...')
    with torch.no_grad():
        for batch_count, data in enumerate(tqdm(test_loader), 0):
            """
            PREPARE INPUT DATA
            """
            try:
                if args['training']['save_vis_results'] and args['training']['eval_mode'] == 'pc':
                    images, images_raw, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, annot_path, heights, widths, triplets, bbox_raw = data
                else:
                    images, _, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, annot_path = data
            except:
                continue

            if prepare_cs_step != 2:
                # we need to run model inference unless we are in the running mode 'prepare_cs' and at step 2 when we have finished accumulating all triplets and need to save them
                Recall.load_annotation_paths(annot_path)

                image_feature = process_image_features(args, images, detr, rank)

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
                    keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)
                    relations_target.append(torch.vstack([relationships[i][graph_iter] for i in keep_in_batch]).T.to(rank))  # integer labels
                    direction_target.append(torch.vstack([subj_or_obj[i][graph_iter] for i in keep_in_batch]).T.to(rank))

                """
                FORWARD PASS THROUGH THE LOCAL PREDICTOR
                """
                num_graph_iter = torch.as_tensor([len(mask) for mask in masks])
                for graph_iter in range(max(num_graph_iter)):
                    keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)

                    curr_graph_masks = torch.stack([torch.unsqueeze(masks[i][graph_iter], dim=0) for i in keep_in_batch])
                    h_graph = torch.cat((image_feature[keep_in_batch] * curr_graph_masks, image_depth[keep_in_batch] * curr_graph_masks), dim=1)  # (bs, 256, 64, 64), (bs, 1, 64, 64)
                    cat_graph = torch.tensor([torch.unsqueeze(categories[i][graph_iter], dim=0) for i in keep_in_batch]).to(rank)
                    spcat_graph = [super_categories[i][graph_iter] for i in keep_in_batch] if super_categories[0] is not None else None
                    bbox_graph = torch.stack([bbox[i][graph_iter] for i in keep_in_batch]).to(rank)

                    for edge_iter in range(graph_iter):
                        curr_edge_masks = torch.stack([torch.unsqueeze(masks[i][edge_iter], dim=0) for i in keep_in_batch])  # seg mask of every prev obj
                        h_edge = torch.cat((image_feature[keep_in_batch] * curr_edge_masks, image_depth[keep_in_batch] * curr_edge_masks), dim=1)
                        cat_edge = torch.tensor([torch.unsqueeze(categories[i][edge_iter], dim=0) for i in keep_in_batch]).to(rank)
                        spcat_edge = [super_categories[i][edge_iter] for i in keep_in_batch] if super_categories[0] is not None else None
                        bbox_edge = torch.stack([bbox[i][edge_iter] for i in keep_in_batch]).to(rank)

                        # filter out subject-object pairs whose iou=0
                        joint_intersect = torch.logical_or(curr_graph_masks, curr_edge_masks)
                        joint_union = torch.logical_and(curr_graph_masks, curr_edge_masks)
                        joint_iou = (torch.sum(torch.sum(joint_intersect, dim=-1), dim=-1) / torch.sum(torch.sum(joint_union, dim=-1), dim=-1)).flatten()
                        joint_iou[torch.isinf(joint_iou)] = 0
                        iou_mask = joint_iou > 0
                        if torch.sum(iou_mask) == 0:
                            continue

                        """
                        FIRST DIRECTION
                        """
                        curr_num_not_connected, curr_num_connected, curr_num_connected_pred, curr_connectivity_precision, curr_connectivity_recall = \
                            evaluate_one_direction(relation_classifier, args, h_graph, h_edge, cat_graph, cat_edge, spcat_graph, spcat_edge, bbox_graph, bbox_edge, iou_mask, rank, graph_iter, edge_iter, keep_in_batch,
                                                   Recall, Recall_top3, relations_target, direction_target, batch_count, len(test_loader))

                        num_not_connected += curr_num_not_connected
                        num_connected += curr_num_connected
                        num_connected_pred += curr_num_connected_pred
                        connectivity_precision += curr_connectivity_precision
                        connectivity_recall += curr_connectivity_recall

                        """
                        SECOND DIRECTION
                        """
                        curr_num_not_connected, curr_num_connected, curr_num_connected_pred, curr_connectivity_precision, curr_connectivity_recall = \
                            evaluate_one_direction(relation_classifier, args, h_edge, h_graph, cat_edge, cat_graph, spcat_edge, spcat_graph, bbox_edge, bbox_graph, iou_mask, rank, graph_iter, edge_iter, keep_in_batch,
                                                   Recall, Recall_top3, relations_target, direction_target, batch_count, len(test_loader))

                        num_not_connected += curr_num_not_connected
                        num_connected += curr_num_connected
                        num_connected_pred += curr_num_connected_pred
                        connectivity_precision += curr_connectivity_precision
                        connectivity_recall += curr_connectivity_recall

                """
                EVALUATE AND PRINT CURRENT RESULTS
                """
                if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                    if args['training']['save_vis_results'] and args['training']['eval_mode'] == 'pc':
                        Recall.save_visualization_results(annot_path, triplets, heights, widths, images_raw, image_depth, bbox, categories, batch_count, top_k=15)

                    if args['dataset']['dataset'] == 'vg':
                        if args['training']['run_mode'] == 'prepare_cs':
                            """
                            To achieve commonsense validation, we run the model on the training set, save the top-k predictions for each image,
                            and then collect a commonsense-aligned set and a commonsense-violated set stored in the form of two .pt files. 
                            Note that the query process to GPT may be interrupted by API, budget limit, or Internet issues, 
                            so we opt to save the results for each image and collect the two sets afterwards.
                            """
                            _, _, cache_hit_percentage = Recall.get_related_top_k_predictions_parallel(top_k=10)
                            if batch_count + 1 == len(test_loader):
                                print('cache hit percentage', cache_hit_percentage)
                        else:
                            recall, recall_per_class, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True)
                        if args['models']['hierarchical_pred']:
                            recall_top3, _, mean_recall_top3 = Recall_top3.compute(per_class=True)
                            Recall_top3.clear_data()
                    else:
                        recall, _, mean_recall, _, _, _ = Recall.compute(per_class=True)
                        wmap_rel, wmap_phrase = Recall.compute_precision()

                    if args['training']['run_mode'] != 'prepare_cs':
                        if (batch_count % args['training']['print_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                            record_test_results(args, test_record, rank, args['training']['test_epoch'], recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs,
                                                connectivity_recall, num_connected, num_not_connected, connectivity_precision, num_connected_pred, wmap_rel, wmap_phrase)
                    # clean up the evaluator
                    Recall.clear_data()

        # on the second step of prepare_cs, we accumulate all the commonsense-aligned and commonsense-violated sets from all images in the training dataset and save them as two .pt files
        if args['training']['run_mode'] == 'prepare_cs' and prepare_cs_step == 2:
            curr_dataset.save_all_triplets()

        dist.monitored_barrier(timeout=datetime.timedelta(seconds=3600))

    Recall.clear_gpt_cache()
    dist.destroy_process_group()  # clean up
    print('FINISHED TESTING PC\n')


def eval_sgd(gpu, args, test_subset):
    """
    This function evaluates the module on scene graph detection tasks.
    :param gpu: current gpu index
    :param args: input arguments in config.yaml
    :param test_subset: testing dataset
    """
    rank = gpu
    world_size = torch.cuda.device_count()
    setup(rank, world_size)
    print('rank', rank, 'torch.distributed.is_initialized', torch.distributed.is_initialized())

    test_sampler = torch.utils.data.distributed.DistributedSampler(test_subset, num_replicas=world_size, rank=rank)
    test_loader = torch.utils.data.DataLoader(test_subset, batch_size=args['training']['batch_size'], shuffle=False, collate_fn=collate_fn, num_workers=0, drop_last=True, sampler=test_sampler)
    print("Finished loading the datasets...")

    start = []
    test_record = []
    with open(args['training']['result_path'] + 'test_results_' + str(rank) + '.json', 'w') as f:  # clear history logs
        json.dump(start, f)

    if args['models']['hierarchical_pred']:
        relation_classifier = DDP(BayesianRelationClassifier(args=args, input_dim=args['models']['hidden_dim'], feature_size=args['models']['feature_size'],
                                                             num_classes=args['models']['num_classes'], num_super_classes=args['models']['num_super_classes'],
                                                             num_geometric=args['models']['num_geometric'], num_possessive=args['models']['num_possessive'],
                                                             num_semantic=args['models']['num_semantic'])).to(rank)
    else:
        relation_classifier = DDP(FlatRelationClassifier(args=args, input_dim=args['models']['hidden_dim'], output_dim=args['models']['num_relations'],
                                                         feature_size=args['models']['feature_size'], num_classes=args['models']['num_classes'])).to(rank)

    detr = DDP(build_detr101(args)).to(rank)
    backbone = DDP(detr.module.backbone).to(rank)
    input_proj = DDP(detr.module.input_proj).to(rank)
    feature_encoder = DDP(detr.module.transformer.encoder).to(rank)

    relation_classifier.eval()
    backbone.eval()
    input_proj.eval()
    feature_encoder.eval()

    map_location = {'cuda:%d' % rank: 'cuda:%d' % 0}
    if args['models']['hierarchical_pred']:
        load_model_name = 'HierRelationModel_CS' if args['training']['run_mode'] == 'prepare_cs' or args['training']['run_mode'] == 'eval_cs' else 'HierRelationModel_Baseline'
        load_model_name = args['training']['checkpoint_path'] + load_model_name + str(args['training']['test_epoch']) + '_0' + '.pth'
    else:
        load_model_name = 'FlatRelationModel_CS' if args['training']['run_mode'] == 'prepare_cs' or args['training']['run_mode'] == 'eval_cs' else 'FlatRelationModel_Baseline'
        load_model_name = args['training']['checkpoint_path'] + load_model_name + str(args['training']['test_epoch']) + '_0' + '.pth'
    if rank == 0:
        print('Loading pretrained model from %s...' % load_model_name)
    relation_classifier.load_state_dict(torch.load(load_model_name, map_location=map_location))

    connectivity_recall, connectivity_precision, num_connected, num_not_connected, num_connected_pred = 0.0, 0.0, 0.0, 0.0, 0.0
    recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs, wmap_rel, wmap_phrase = None, None, None, None, None, None, None, None

    Recall = Evaluator(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])

    sub2super_cat_dict = torch.load(args['dataset']['sub2super_cat_dict'])
    object_class_alp2fre_dict = object_class_alp2fre()

    print('Start Testing SGD...')
    with torch.no_grad():
        for batch_count, data in enumerate(tqdm(test_loader), 0):
            """
            PREPARE INPUT DATA WITH PREDICTED OBJECT BOUNDING BOXES AND LABELS
            """
            try:
                images, image2, image_depth, categories_target, super_categories_target, bbox_target, relationships, subj_or_obj, _ = data
            except:
                continue

            image_feature = process_image_features(args, images, detr, rank)

            image_depth = torch.stack([depth.to(rank) for depth in image_depth])
            categories_target = [category.to(rank) for category in categories_target]  # [batch_size][curr_num_obj, 1]
            bbox_target = [box.to(rank) for box in bbox_target]  # [batch_size][curr_num_obj, 4]

            image2 = [image.to(rank) for image in image2]
            out_dict = detr(nested_tensor_from_tensor_list(image2))

            logits_pred = torch.argmax(F.softmax(out_dict['pred_logits'], dim=2), dim=2)
            has_object_pred = logits_pred < args['models']['num_classes']
            logits_pred = torch.topk(F.softmax(out_dict['pred_logits'], dim=2), dim=2, k=args['models']['topk_cat'])[1].view(-1, 100, args['models']['topk_cat'])

            logits_pred_value = torch.topk(F.softmax(out_dict['pred_logits'], dim=2), dim=2, k=args['models']['topk_cat'])[0].view(-1, 100, args['models']['topk_cat'])
            cat_pred_confidence = [logits_pred_value[i, has_object_pred[i], :].flatten() for i in range(logits_pred_value.shape[0]) if torch.sum(has_object_pred[i]) > 0]

            categories_pred = [logits_pred[i, has_object_pred[i], :].flatten() for i in range(logits_pred.shape[0]) if torch.sum(has_object_pred[i]) > 0]  # (batch_size, num_obj, 150)
            # object category indices in pretrained DETR are different from our indices
            for i in range(len(categories_pred)):
                for j in range(len(categories_pred[i])):
                    categories_pred[i][j] = object_class_alp2fre_dict[categories_pred[i][j].item()]     # at this moment, keep cat whose top2 == 150 for convenience
            cat_mask = [categories_pred[i] != args['models']['num_classes'] for i in range(len(categories_pred))]

            bbox_pred = [out_dict['pred_boxes'][i, has_object_pred[i]] for i in range(logits_pred.shape[0]) if torch.sum(has_object_pred[i]) > 0]  # convert from 0-1 to 0-32
            for i in range(len(bbox_pred)):
                # clone and calculate the new bounding box coordinates predicted by DETR
                bbox_pred_c = bbox_pred[i].clone()
                bbox_pred[i][:, [0, 2]] = bbox_pred_c[:, [0, 1]] - bbox_pred_c[:, [2, 3]] / 2
                bbox_pred[i][:, [1, 3]] = bbox_pred_c[:, [0, 1]] + bbox_pred_c[:, [2, 3]] / 2
                bbox_pred[i] = torch.clamp(bbox_pred[i], 0, 1)
                bbox_pred[i] = (bbox_pred[i] * args['models']['feature_size']).repeat_interleave(args['models']['topk_cat'], dim=0)

            masks_pred = []
            for i in range(len(bbox_pred)):
                mask_pred = torch.zeros(bbox_pred[i].shape[0], args['models']['feature_size'], args['models']['feature_size'], dtype=torch.uint8).to(rank)
                for j, box in enumerate(bbox_pred[i]):
                    mask_pred[j, int(bbox_pred[i][j][2]):int(bbox_pred[i][j][3]), int(bbox_pred[i][j][0]):int(bbox_pred[i][j][1])] = 1
                masks_pred.append(mask_pred)

            for i in range(len(categories_pred)):
                categories_pred[i] = categories_pred[i][cat_mask[i]]
                cat_pred_confidence[i] = cat_pred_confidence[i][cat_mask[i]]
                bbox_pred[i] = bbox_pred[i][cat_mask[i]]
                masks_pred[i] = masks_pred[i][cat_mask[i]]

            # non-maximum suppression
            for i in range(len(bbox_pred)):
                bbox_pred[i] = bbox_pred[i][:, [0, 2, 1, 3]]

                nms_keep_idx = None
                for cls in torch.unique(categories_pred[i]):  # per class nms
                    curr_class_idx = categories_pred[i] == cls
                    curr_nms_keep_idx = torchvision.ops.nms(boxes=bbox_pred[i][curr_class_idx], scores=cat_pred_confidence[i][curr_class_idx],
                                                            iou_threshold=args['models']['nms'])       # requires (x1, y1, x2, y2)
                    if nms_keep_idx is None:
                        nms_keep_idx = (torch.nonzero(curr_class_idx).flatten())[curr_nms_keep_idx]
                    else:
                        nms_keep_idx = torch.hstack((nms_keep_idx, (torch.nonzero(curr_class_idx).flatten())[curr_nms_keep_idx]))

                bbox_pred[i] = bbox_pred[i][:, [0, 2, 1, 3]]       # convert back to (x1, x2, y1, y2)
                categories_pred[i] = categories_pred[i][nms_keep_idx]
                cat_pred_confidence[i] = cat_pred_confidence[i][nms_keep_idx]
                bbox_pred[i] = bbox_pred[i][nms_keep_idx]
                masks_pred[i] = masks_pred[i][nms_keep_idx]

            # after nms
            super_categories_pred = [[sub2super_cat_dict[c.item()] for c in categories_pred[i]] for i in range(len(categories_pred))]
            super_categories_pred = [[torch.as_tensor(sc).to(rank) for sc in super_category] for super_category in super_categories_pred]

            """
            PREPARE TARGETS
            relations_target and direction_target: matched targets for each prediction
            cat_subject_target, cat_object_target, bbox_subject_target, bbox_object_target, relation_target_origin: sets of original unmatched targets
            """
            cat_subject_target, cat_object_target, bbox_subject_target, bbox_object_target, relation_target \
                = match_target_sgd(rank, relationships, subj_or_obj, categories_target, bbox_target)

            """
            FORWARD PASS THROUGH THE LOCAL PREDICTOR
            """
            num_graph_iter = torch.as_tensor([len(mask) for mask in masks_pred])
            for graph_iter in range(max(num_graph_iter)):
                keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1).to(rank)

                curr_graph_masks = torch.stack([torch.unsqueeze(masks_pred[i][graph_iter], dim=0) for i in keep_in_batch])
                h_graph = torch.cat((image_feature[keep_in_batch] * curr_graph_masks, image_depth[keep_in_batch] * curr_graph_masks), dim=1)  # (bs, 256, 64, 64), (bs, 1, 64, 64)
                cat_graph_pred = torch.tensor([torch.unsqueeze(categories_pred[i][graph_iter], dim=0) for i in keep_in_batch]).to(rank)
                bbox_graph_pred = torch.stack([bbox_pred[i][graph_iter] for i in keep_in_batch]).to(rank)
                cat_graph_confidence = torch.hstack([cat_pred_confidence[i][graph_iter] for i in keep_in_batch])

                for edge_iter in range(graph_iter):
                    curr_edge_masks = torch.stack([torch.unsqueeze(masks_pred[i][edge_iter], dim=0) for i in keep_in_batch])  # seg mask of every prev obj
                    h_edge = torch.cat((image_feature[keep_in_batch] * curr_edge_masks, image_depth[keep_in_batch] * curr_edge_masks), dim=1)
                    cat_edge_pred = torch.tensor([torch.unsqueeze(categories_pred[i][edge_iter], dim=0) for i in keep_in_batch]).to(rank)
                    bbox_edge_pred = torch.stack([bbox_pred[i][edge_iter] for i in keep_in_batch]).to(rank)

                    cat_edge_confidence = torch.hstack([cat_pred_confidence[i][edge_iter] for i in keep_in_batch])

                    # filter out subject-object pairs whose iou=0
                    joint_intersect = torch.logical_or(curr_graph_masks, curr_edge_masks)
                    joint_union = torch.logical_and(curr_graph_masks, curr_edge_masks)
                    joint_iou = (torch.sum(torch.sum(joint_intersect, dim=-1), dim=-1) / torch.sum(torch.sum(joint_union, dim=-1), dim=-1)).flatten()
                    joint_iou[torch.isinf(joint_iou)] = 0
                    iou_mask = joint_iou > 0

                    if torch.sum(iou_mask) == 0:
                        continue

                    spcat_graph_pred = []    # they are not tensors but lists, which requires special mask manipulations
                    spcat_edge_pred = []
                    for count, i in enumerate(keep_in_batch):
                        spcat_graph_pred.append(super_categories_pred[i][graph_iter])
                        spcat_edge_pred.append(super_categories_pred[i][edge_iter])

                    """
                    FIRST DIRECTION
                    """
                    if args['models']['hierarchical_pred']:
                        relation_1, relation_2, relation_3, super_relation, connectivity, _, _ = relation_classifier(h_graph, h_edge, cat_graph_pred, cat_edge_pred, spcat_graph_pred, spcat_edge_pred, rank)
                        relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
                    else:
                        relation, connectivity, _, _ = relation_classifier(h_graph, h_edge, cat_graph_pred, cat_edge_pred, spcat_graph_pred, spcat_edge_pred, rank)
                        super_relation = None

                    if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                        Recall.accumulate(keep_in_batch, relation, None, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                          cat_graph_pred, cat_edge_pred, None, None, bbox_graph_pred, bbox_edge_pred, None, None,
                                          iou_mask, False, cat_graph_confidence, cat_edge_confidence)
                    """
                    SECOND DIRECTION
                    """
                    if args['models']['hierarchical_pred']:
                        relation_1, relation_2, relation_3, super_relation, connectivity, _, _ = relation_classifier(h_edge, h_graph, cat_edge_pred, cat_graph_pred, spcat_edge_pred, spcat_graph_pred, rank)
                        relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
                    else:
                        relation, connectivity, _, _ = relation_classifier(h_edge, h_graph, cat_edge_pred, cat_graph_pred, spcat_edge_pred, spcat_graph_pred, rank)
                        super_relation = None

                    if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                        Recall.accumulate(keep_in_batch, relation, None, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                          cat_edge_pred, cat_graph_pred, None, None, bbox_edge_pred, bbox_graph_pred, None, None,
                                          iou_mask, False, cat_edge_confidence, cat_graph_confidence)

            if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                Recall.accumulate_target(relation_target, cat_subject_target, cat_object_target, bbox_subject_target, bbox_object_target)
            """
            EVALUATE AND PRINT CURRENT RESULTS
            """
            if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                recall, recall_per_class, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True, predcls=False)
                Recall.clear_data()

                if (batch_count % args['training']['print_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                    record_test_results(args, test_record, rank, args['training']['test_epoch'], recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs,
                                        connectivity_recall, num_connected, num_not_connected, connectivity_precision, num_connected_pred, wmap_rel, wmap_phrase)

        dist.monitored_barrier(timeout=datetime.timedelta(seconds=3600))

    print('FINISHED TESTING SGD\n')
    dist.destroy_process_group()  # clean up


def eval_sgc(gpu, args, test_subset):
    """
    This function evaluates the module on scene graph classification tasks.
    :param gpu: current gpu index
    :param args: input arguments in config.yaml
    :param test_subset: testing dataset
    """
    rank = gpu
    world_size = torch.cuda.device_count()
    setup(rank, world_size)
    print('rank', rank, 'torch.distributed.is_initialized', torch.distributed.is_initialized())

    test_sampler = torch.utils.data.distributed.DistributedSampler(test_subset, num_replicas=world_size, rank=rank)
    test_loader = torch.utils.data.DataLoader(test_subset, batch_size=args['training']['batch_size'], shuffle=False, collate_fn=collate_fn, num_workers=0, drop_last=True, sampler=test_sampler)
    print("Finished loading the datasets...")

    start = []
    test_record = []
    with open(args['training']['result_path'] + 'test_results_' + str(rank) + '.json', 'w') as f:  # clear history logs
        json.dump(start, f)

    if args['models']['hierarchical_pred']:
        relation_classifier = DDP(BayesianRelationClassifier(args=args, input_dim=args['models']['hidden_dim'], feature_size=args['models']['feature_size'],
                                                             num_classes=args['models']['num_classes'], num_super_classes=args['models']['num_super_classes'],
                                                             num_geometric=args['models']['num_geometric'], num_possessive=args['models']['num_possessive'],
                                                             num_semantic=args['models']['num_semantic'])).to(rank)
    else:
        relation_classifier = DDP(FlatRelationClassifier(args=args, input_dim=args['models']['hidden_dim'], output_dim=args['models']['num_relations'],
                                                         feature_size=args['models']['feature_size'], num_classes=args['models']['num_classes'])).to(rank)

    detr = DDP(build_detr101(args)).to(rank)
    backbone = DDP(detr.module.backbone).to(rank)
    input_proj = DDP(detr.module.input_proj).to(rank)
    feature_encoder = DDP(detr.module.transformer.encoder).to(rank)

    relation_classifier.eval()
    backbone.eval()
    input_proj.eval()
    feature_encoder.eval()

    map_location = {'cuda:%d' % rank: 'cuda:%d' % 0}
    if args['models']['hierarchical_pred']:
        load_model_name = 'HierRelationModel_CS' if args['training']['run_mode'] == 'prepare_cs' or args['training']['run_mode'] == 'eval_cs' else 'HierRelationModel_Baseline'
        load_model_name = args['training']['checkpoint_path'] + load_model_name + str(args['training']['test_epoch']) + '_0' + '.pth'
    else:
        load_model_name = 'FlatRelationModel_CS' if args['training']['run_mode'] == 'prepare_cs' or args['training']['run_mode'] == 'eval_cs' else 'FlatRelationModel_Baseline'
        load_model_name = args['training']['checkpoint_path'] + load_model_name + str(args['training']['test_epoch']) + '_0' + '.pth'
    if rank == 0:
        print('Loading pretrained model from %s...' % load_model_name)
    relation_classifier.load_state_dict(torch.load(load_model_name, map_location=map_location))

    Recall = Evaluator(args=args, num_classes=args['models']['num_relations'], iou_thresh=0.5, top_k=[20, 50, 100])

    connectivity_recall, connectivity_precision, num_connected, num_not_connected, num_connected_pred = 0.0, 0.0, 0.0, 0.0, 0.0
    recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs, wmap_rel, wmap_phrase = None, None, None, None, None, None, None, None

    sub2super_cat_dict = torch.load(args['dataset']['sub2super_cat_dict'])
    object_class_alp2fre_dict = object_class_alp2fre()

    print('Start Testing SGC...')
    with torch.no_grad():
        for batch_count, data in enumerate(tqdm(test_loader), 0):
            """
            PREPARE INPUT DATA WITH PREDICTED OBJECT BOUNDING BOXES AND LABELS
            """
            try:
                images, image2, image_depth, categories_target, super_categories_target, bbox_target, relationships, subj_or_obj, _ = data
            except:
                continue

            image_feature = process_image_features(args, images, detr, rank)

            image_depth = torch.stack([depth.to(rank) for depth in image_depth])
            categories_target = [category.to(rank) for category in categories_target]  # [batch_size][curr_num_obj, 1]
            bbox_target = [box.to(rank) for box in bbox_target]  # [batch_size][curr_num_obj, 4]

            image2 = [image.to(rank) for image in image2]
            out_dict = detr(nested_tensor_from_tensor_list(image2))

            logits_pred = torch.argmax(F.softmax(out_dict['pred_logits'], dim=2), dim=2)
            has_object_pred = logits_pred < args['models']['num_classes']
            logits_pred = torch.topk(F.softmax(out_dict['pred_logits'], dim=2), dim=2, k=args['models']['topk_cat'])[1].view(-1, 100, args['models']['topk_cat'])

            logits_pred_value = torch.topk(F.softmax(out_dict['pred_logits'], dim=2), dim=2, k=args['models']['topk_cat'])[0].view(-1, 100, args['models']['topk_cat'])
            cat_pred_confidence = [logits_pred_value[i, has_object_pred[i], :].flatten() for i in range(logits_pred_value.shape[0]) if torch.sum(has_object_pred[i]) > 0]

            categories_pred = [logits_pred[i, has_object_pred[i], :].flatten() for i in range(logits_pred.shape[0]) if torch.sum(has_object_pred[i]) > 0]  # (batch_size, num_obj, 150)
            # object category indices in pretrained DETR are different from our indices
            for i in range(len(categories_pred)):
                for j in range(len(categories_pred[i])):
                    categories_pred[i][j] = object_class_alp2fre_dict[categories_pred[i][j].item()]     # at this moment, keep cat whose top2 == 150 for convenience
            cat_mask = [categories_pred[i] != args['models']['num_classes'] for i in range(len(categories_pred))]

            bbox_pred = [out_dict['pred_boxes'][i, has_object_pred[i]] for i in range(logits_pred.shape[0]) if torch.sum(has_object_pred[i]) > 0]  # convert from 0-1 to 0-32
            for i in range(len(bbox_pred)):
                # clone and calculate the new bounding box coordinates predicted by DETR
                bbox_pred_c = bbox_pred[i].clone()
                bbox_pred[i][:, [0, 2]] = bbox_pred_c[:, [0, 1]] - bbox_pred_c[:, [2, 3]] / 2
                bbox_pred[i][:, [1, 3]] = bbox_pred_c[:, [0, 1]] + bbox_pred_c[:, [2, 3]] / 2
                bbox_pred[i] = torch.clamp(bbox_pred[i], 0, 1)
                bbox_pred[i] = (bbox_pred[i] * args['models']['feature_size']).repeat_interleave(args['models']['topk_cat'], dim=0)

            for i in range(len(categories_pred)):
                categories_pred[i] = categories_pred[i][cat_mask[i]]
                cat_pred_confidence[i] = cat_pred_confidence[i][cat_mask[i]]
                bbox_pred[i] = bbox_pred[i][cat_mask[i]]

            # non-maximum suppression
            for i in range(len(bbox_pred)):
                bbox_pred[i] = bbox_pred[i][:, [0, 2, 1, 3]]

                nms_keep_idx = None
                for cls in torch.unique(categories_pred[i]):  # per class nms
                    curr_class_idx = categories_pred[i] == cls
                    curr_nms_keep_idx = torchvision.ops.nms(boxes=bbox_pred[i][curr_class_idx], scores=cat_pred_confidence[i][curr_class_idx],
                                                            iou_threshold=args['models']['nms'])       # requires (x1, y1, x2, y2)
                    if nms_keep_idx is None:
                        nms_keep_idx = (torch.nonzero(curr_class_idx).flatten())[curr_nms_keep_idx]
                    else:
                        nms_keep_idx = torch.hstack((nms_keep_idx, (torch.nonzero(curr_class_idx).flatten())[curr_nms_keep_idx]))

                bbox_pred[i] = bbox_pred[i][:, [0, 2, 1, 3]]       # convert back to (x1, x2, y1, y2)
                categories_pred[i] = categories_pred[i][nms_keep_idx]
                cat_pred_confidence[i] = cat_pred_confidence[i][nms_keep_idx]
                bbox_pred[i] = bbox_pred[i][nms_keep_idx]

            """
            PREPARE TARGETS
            relations_target and direction_target: matched targets for each prediction
            cat_subject_target, cat_object_target, bbox_subject_target, bbox_object_target, relation_target_origin: sets of original unmatched targets
            """
            cat_subject_target, cat_object_target, bbox_subject_target, bbox_object_target, relation_target \
                = match_target_sgd(rank, relationships, subj_or_obj, categories_target, bbox_target)

            """
            MATCH PREDICTED OBJECT LABELS FROM BOUNDING BOX IOUS
            """
            categories_pred_matched, cat_pred_confidence_matched, bbox_target = match_object_categories(categories_pred, cat_pred_confidence, bbox_pred, bbox_target)
            if categories_pred_matched is None or cat_pred_confidence_matched is None:
                continue

            # bbox_target = [torch.repeat_interleave(box, repeats=2, dim=0) for box in bbox_target]
            assert len(categories_pred_matched[0]) == len(bbox_target[0])

            # after nms
            super_categories_pred = [[sub2super_cat_dict[c.item()] for c in categories_pred_matched[i]] for i in range(len(categories_pred_matched))]
            super_categories_pred = [[torch.as_tensor(sc).to(rank) for sc in super_category] for super_category in super_categories_pred]

            masks_target = []
            for i in range(len(bbox_target)):
                mask = torch.zeros(bbox_target[i].shape[0], args['models']['feature_size'], args['models']['feature_size'], dtype=torch.uint8).to(rank)
                for j, box in enumerate(bbox_target[i]):
                    mask[j, int(bbox_target[i][j][2]):int(bbox_target[i][j][3]), int(bbox_target[i][j][0]):int(bbox_target[i][j][1])] = 1
                masks_target.append(mask)

            """
            FORWARD PASS THROUGH THE LOCAL PREDICTOR
            """
            num_graph_iter = torch.as_tensor([len(mask) for mask in masks_target])
            for graph_iter in range(max(num_graph_iter)):
                keep_in_batch = torch.nonzero(num_graph_iter > graph_iter).view(-1)

                curr_graph_masks = torch.stack([torch.unsqueeze(masks_target[i][graph_iter], dim=0) for i in keep_in_batch])
                h_graph = torch.cat((image_feature[keep_in_batch] * curr_graph_masks, image_depth[keep_in_batch] * curr_graph_masks), dim=1)  # (bs, 256, 64, 64), (bs, 1, 64, 64)
                cat_graph_pred = torch.tensor([torch.unsqueeze(categories_pred_matched[i][graph_iter], dim=0) for i in keep_in_batch]).to(rank)
                bbox_graph_pred = torch.stack([bbox_target[i][graph_iter] for i in keep_in_batch]).to(rank)    # use ground-truth bounding boxes
                cat_graph_confidence = torch.hstack([cat_pred_confidence_matched[i][graph_iter] for i in keep_in_batch])

                for edge_iter in range(graph_iter):
                    curr_edge_masks = torch.stack([torch.unsqueeze(masks_target[i][edge_iter], dim=0) for i in keep_in_batch])  # seg mask of every prev obj
                    h_edge = torch.cat((image_feature[keep_in_batch] * curr_edge_masks, image_depth[keep_in_batch] * curr_edge_masks), dim=1)
                    cat_edge_pred = torch.tensor([torch.unsqueeze(categories_pred_matched[i][edge_iter], dim=0) for i in keep_in_batch]).to(rank)
                    bbox_edge_pred = torch.stack([bbox_target[i][edge_iter] for i in keep_in_batch]).to(rank)    # use ground-truth bounding boxes
                    cat_edge_confidence = torch.hstack([cat_pred_confidence_matched[i][edge_iter] for i in keep_in_batch])

                    # filter out subject-object pairs whose iou=0
                    joint_intersect = torch.logical_or(curr_graph_masks, curr_edge_masks)
                    joint_union = torch.logical_and(curr_graph_masks, curr_edge_masks)
                    joint_iou = (torch.sum(torch.sum(joint_intersect, dim=-1), dim=-1) / torch.sum(torch.sum(joint_union, dim=-1), dim=-1)).flatten()
                    joint_iou[torch.isinf(joint_iou)] = 0
                    iou_mask = joint_iou > 0
                    if torch.sum(iou_mask) == 0:
                        continue

                    spcat_graph_pred = []  # they are not tensors but lists, which requires special mask manipulations
                    spcat_edge_pred = []
                    for count, i in enumerate(keep_in_batch):
                        spcat_graph_pred.append(super_categories_pred[i][graph_iter])
                        spcat_edge_pred.append(super_categories_pred[i][edge_iter])

                    """
                    FIRST DIRECTION
                    """
                    with torch.no_grad():
                        if args['models']['hierarchical_pred']:
                            relation_1, relation_2, relation_3, super_relation, connectivity, _, _ = relation_classifier(h_graph, h_edge, cat_graph_pred, cat_edge_pred, spcat_graph_pred, spcat_edge_pred, rank)
                            relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
                        else:
                            relation, connectivity, _, _ = relation_classifier(h_graph, h_edge, cat_graph_pred, cat_edge_pred, spcat_graph_pred, spcat_edge_pred, rank)
                            super_relation = None

                    if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                            Recall.accumulate(keep_in_batch, relation, None, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                              cat_graph_pred, cat_edge_pred, None, None, bbox_graph_pred, bbox_edge_pred, None, None,
                                              iou_mask, False, cat_graph_confidence, cat_edge_confidence)
                    """
                    SECOND DIRECTION
                    """
                    with torch.no_grad():
                        if args['models']['hierarchical_pred']:
                            relation_1, relation_2, relation_3, super_relation, connectivity, _, _ = relation_classifier(h_edge, h_graph, cat_edge_pred, cat_graph_pred, spcat_edge_pred, spcat_graph_pred, rank)
                            relation = torch.cat((relation_1, relation_2, relation_3), dim=1)
                        else:
                            relation, connectivity, _, _ = relation_classifier(h_edge, h_graph, cat_edge_pred, cat_graph_pred, spcat_edge_pred, spcat_graph_pred, rank)
                            super_relation = None

                    if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                        Recall.accumulate(keep_in_batch, relation, None, super_relation, torch.log(torch.sigmoid(connectivity[:, 0])),
                                               cat_edge_pred, cat_graph_pred, None, None, bbox_edge_pred, bbox_graph_pred, None, None,
                                               iou_mask, False, cat_edge_confidence, cat_graph_confidence)

            if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                Recall.accumulate_target(relation_target, cat_subject_target, cat_object_target, bbox_subject_target, bbox_object_target)

            """
            EVALUATE AND PRINT CURRENT RESULTS
            """
            if (batch_count % args['training']['eval_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                recall, recall_per_class, mean_recall, recall_zs, _, mean_recall_zs = Recall.compute(per_class=True, predcls=False)
                Recall.clear_data()

            if (batch_count % args['training']['print_freq_test'] == 0) or (batch_count + 1 == len(test_loader)):
                record_test_results(args, test_record, rank, args['training']['test_epoch'], recall_top3, recall, mean_recall_top3, mean_recall, recall_zs, mean_recall_zs,
                                    connectivity_recall, num_connected, num_not_connected, connectivity_precision, num_connected_pred, wmap_rel, wmap_phrase)

    dist.monitored_barrier()
    print('FINISHED TESTING SGC\n')

