import torch
import torch.nn.functional as F
import numpy as np
from tqdm import tqdm
import math

from utils import get_weight_oiv6
from dataset_utils import relation_by_super_class_int2str, object_class_int2str


class Evaluator_PC:
    """
    The class evaluate the model performance on Recall@k and mean Recall@k evaluation metrics on predicate classification tasks.
    In our hierarchical relationship scheme, each edge has three predictions per direction under three disjoint super-categories.
    Therefore, each directed edge outputs three individual candidates to be ranked in the top k most confident predictions instead of one.
    """
    def __init__(self, args, num_classes, iou_thresh, top_k):
        self.args = args
        self.hierar = args['models']['hierarchical_pred']
        self.top_k = top_k
        self.num_classes = num_classes
        self.iou_thresh = iou_thresh
        self.num_connected_target = 0.0
        self.motif_total = 0.0
        self.motif_correct = 0.0
        self.result_dict = {20: 0.0, 50: 0.0, 100: 0.0}
        self.result_per_class = {k: torch.tensor([0.0 for i in range(self.num_classes)]) for k in self.top_k}
        self.num_conn_target_per_class = torch.tensor([0.0 for i in range(self.num_classes)])

        if args['dataset']['dataset'] == 'vg':
            self.train_triplets = torch.load(args['dataset']['train_triplets'])
            self.test_triplets = torch.load(args['dataset']['test_triplets'])
            self.zero_shot_triplets = torch.load(args['dataset']['zero_shot_triplets'])
            self.result_dict_zs = {20: 0.0, 50: 0.0, 100: 0.0}
            self.result_per_class_zs = {k: torch.tensor([0.0 for i in range(self.num_classes)]) for k in self.top_k}
            self.num_connected_target_zs = 0.0
            self.num_conn_target_per_class_zs = torch.tensor([0.0 for i in range(self.num_classes)])
        elif args['dataset']['dataset'] == 'oiv6':
            self.result_per_class_ap = torch.tensor([0.0 for i in range(self.num_classes)])
            self.result_per_class_ap_union = torch.tensor([0.0 for i in range(self.num_classes)])
            self.num_conn_target_per_class_ap = torch.tensor([0.0 for i in range(self.num_classes)])

        self.which_in_batch = None
        # self.connected_pred = None
        self.confidence = None
        self.connectivity = None
        self.relation_pred = None
        self.relation_target = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None


    def iou(self, bbox_target, bbox_pred):
        mask_pred = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_pred[int(bbox_pred[2]):int(bbox_pred[3]), int(bbox_pred[0]):int(bbox_pred[1])] = 1
        mask_target = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_target[int(bbox_target[2]):int(bbox_target[3]), int(bbox_target[0]):int(bbox_target[1])] = 1
        intersect = torch.sum(torch.logical_and(mask_target, mask_pred))
        union = torch.sum(torch.logical_or(mask_target, mask_pred))
        if union == 0:
            return 0
        else:
            return float(intersect) / float(union)


    def iou_union(self, bbox_pred1, bbox_pred2, bbox_target1, bbox_target2):
        mask_pred1 = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_pred1[int(bbox_pred1[2]):int(bbox_pred1[3]), int(bbox_pred1[0]):int(bbox_pred1[1])] = 1
        mask_pred2 = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_pred2[int(bbox_pred2[2]):int(bbox_pred2[3]), int(bbox_pred2[0]):int(bbox_pred2[1])] = 1
        mask_pred = torch.logical_or(mask_pred1, mask_pred2)

        mask_target1 = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_target1[int(bbox_target1[2]):int(bbox_target1[3]), int(bbox_target1[0]):int(bbox_target1[1])] = 1
        mask_target2 = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_target2[int(bbox_target2[2]):int(bbox_target2[3]), int(bbox_target2[0]):int(bbox_target2[1])] = 1
        mask_target = torch.logical_or(mask_target1, mask_target2)

        intersect = torch.sum(torch.logical_and(mask_target, mask_pred))
        union = torch.sum(torch.logical_or(mask_target, mask_pred))
        if union == 0:
            return 0
        else:
            return float(intersect) / float(union)


    def accumulate(self, which_in_batch, relation_pred, relation_target, super_relation_pred, connectivity,
                   subject_cat_pred, object_cat_pred, subject_cat_target, object_cat_target,
                   subject_bbox_pred, object_bbox_pred, subject_bbox_target, object_bbox_target):

        if self.relation_pred is None:
            if not self.hierar:     # flat relationship prediction
                self.which_in_batch = which_in_batch
                # self.connected_pred = torch.exp(connectivity)
                self.connectivity = connectivity
                self.confidence = torch.max(relation_pred, dim=1)[0]
                # self.confidence = torch.max(relation_pred, dim=1)[0]

                self.relation_pred = torch.argmax(relation_pred, dim=1)
                self.relation_target = relation_target

                self.subject_cat_pred = subject_cat_pred
                self.object_cat_pred = object_cat_pred
                self.subject_cat_target = subject_cat_target
                self.object_cat_target = object_cat_target

                self.subject_bbox_pred = subject_bbox_pred
                self.object_bbox_pred = object_bbox_pred
                self.subject_bbox_target = subject_bbox_target
                self.object_bbox_target = object_bbox_target
            else:
                self.which_in_batch = which_in_batch.repeat(3)
                self.confidence = torch.hstack((torch.max(relation_pred[:, :self.args['models']['num_geometric']], dim=1)[0],
                                                torch.max(relation_pred[:, self.args['models']['num_geometric']:
                                                                           self.args['models']['num_geometric'] + self.args['models']['num_possessive']], dim=1)[0],
                                                torch.max(relation_pred[:, self.args['models']['num_geometric'] + self.args['models']['num_possessive']:], dim=1)[0]))
                self.connectivity = connectivity.repeat(3)
                # self.connected_pred = torch.exp(connectivity).repeat(3)

                self.relation_pred = torch.hstack((torch.argmax(relation_pred[:, :self.args['models']['num_geometric']], dim=1),
                                                   torch.argmax(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1)
                                                   + self.args['models']['num_geometric'],
                                                   torch.argmax(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1)
                                                   + self.args['models']['num_geometric'] + self.args['models']['num_possessive']))
                self.relation_target = relation_target.repeat(3)

                self.subject_cat_pred = subject_cat_pred.repeat(3)
                self.object_cat_pred = object_cat_pred.repeat(3)
                self.subject_cat_target = subject_cat_target.repeat(3)
                self.object_cat_target = object_cat_target.repeat(3)

                self.subject_bbox_pred = subject_bbox_pred.repeat(3, 1)
                self.object_bbox_pred = object_bbox_pred.repeat(3, 1)
                self.subject_bbox_target = subject_bbox_target.repeat(3, 1)
                self.object_bbox_target = object_bbox_target.repeat(3, 1)
        else:
            if not self.hierar:     # flat relationship prediction
                self.which_in_batch = torch.hstack((self.which_in_batch, which_in_batch))
                # confidence = connectivity + torch.max(relation_pred, dim=1)[0]
                # confidence = torch.max(relation_pred, dim=1)[0]
                self.confidence = torch.hstack((self.confidence, torch.max(relation_pred, dim=1)[0]))
                self.connectivity = torch.hstack((self.connectivity, connectivity))
                # self.connected_pred = torch.hstack((self.connected_pred, torch.exp(connectivity)))

                self.relation_pred = torch.hstack((self.relation_pred, torch.argmax(relation_pred, dim=1)))
                self.relation_target = torch.hstack((self.relation_target, relation_target))

                self.subject_cat_pred = torch.hstack((self.subject_cat_pred, subject_cat_pred))
                self.object_cat_pred = torch.hstack((self.object_cat_pred, object_cat_pred))
                self.subject_cat_target = torch.hstack((self.subject_cat_target, subject_cat_target))
                self.object_cat_target = torch.hstack((self.object_cat_target, object_cat_target))

                self.subject_bbox_pred = torch.vstack((self.subject_bbox_pred, subject_bbox_pred))
                self.object_bbox_pred = torch.vstack((self.object_bbox_pred, object_bbox_pred))
                self.subject_bbox_target = torch.vstack((self.subject_bbox_target, subject_bbox_target))
                self.object_bbox_target = torch.vstack((self.object_bbox_target, object_bbox_target))
            else:
                self.which_in_batch = torch.hstack((self.which_in_batch, which_in_batch.repeat(3)))
                confidence = torch.hstack((torch.max(relation_pred[:, :self.args['models']['num_geometric']], dim=1)[0],
                                           torch.max(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']
                                                                                                           + self.args['models']['num_possessive']], dim=1)[0],
                                           torch.max(relation_pred[:, self.args['models']['num_geometric'] + self.args['models']['num_possessive']:], dim=1)[0]))
                # confidence += connectivity.repeat(3)
                self.confidence = torch.hstack((self.confidence, confidence))
                self.connectivity = torch.hstack((self.connectivity, connectivity.repeat(3)))
                # connectivity_pred = torch.exp(connectivity).repeat(3)
                # self.connected_pred = torch.hstack((self.connected_pred, connectivity_pred))

                relation_pred_candid = torch.hstack((torch.argmax(relation_pred[:, :self.args['models']['num_geometric']], dim=1),
                                                     torch.argmax(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1)
                                                     + self.args['models']['num_geometric'],
                                                     torch.argmax(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1)
                                                     + self.args['models']['num_geometric'] + self.args['models']['num_possessive']))
                self.relation_pred = torch.hstack((self.relation_pred, relation_pred_candid))
                self.relation_target = torch.hstack((self.relation_target, relation_target.repeat(3)))

                self.subject_cat_pred = torch.hstack((self.subject_cat_pred, subject_cat_pred.repeat(3)))
                self.object_cat_pred = torch.hstack((self.object_cat_pred, object_cat_pred.repeat(3)))
                self.subject_cat_target = torch.hstack((self.subject_cat_target, subject_cat_target.repeat(3)))
                self.object_cat_target = torch.hstack((self.object_cat_target, object_cat_target.repeat(3)))

                self.subject_bbox_pred = torch.vstack((self.subject_bbox_pred, subject_bbox_pred.repeat(3, 1)))
                self.object_bbox_pred = torch.vstack((self.object_bbox_pred, object_bbox_pred.repeat(3, 1)))
                self.subject_bbox_target = torch.vstack((self.subject_bbox_target, subject_bbox_target.repeat(3, 1)))
                self.object_bbox_target = torch.vstack((self.object_bbox_target, object_bbox_target.repeat(3, 1)))


    def get_top_k_predictions(self, top_k):
        """
        Returns the top k most confident predictions for each image in the format: (subject_id, relation_id, object_id).
        """
        top_k_predictions = []
        top_k_image_graphs = []
        dict_relation_names = relation_by_super_class_int2str()
        dict_object_names = object_class_int2str()

        for image in torch.unique(self.which_in_batch):  # image-wise
            curr_image = self.which_in_batch == image
            curr_confidence = self.confidence[curr_image]
            sorted_inds = torch.argsort(curr_confidence, dim=0, descending=True)

            # select the top k predictions
            this_k = min(top_k, len(self.relation_pred[curr_image]))
            keep_inds = sorted_inds[:this_k]

            curr_predictions = []
            curr_image_graph = []

            for ind in keep_inds:
                subject_id = self.subject_cat_pred[curr_image][ind].item()
                relation_id = self.relation_pred[curr_image][ind].item()
                object_id = self.object_cat_pred[curr_image][ind].item()

                subject_bbox = self.subject_bbox_pred[curr_image][ind].cpu()# / self.args['models']['feature_size']
                object_bbox = self.object_bbox_pred[curr_image][ind].cpu()# / self.args['models']['feature_size']
                # subject_bbox[:2] *= height
                # subject_bbox[2:] *= width
                # object_bbox[:2] *= height
                # object_bbox[2:] *= width

                curr_predictions.append(dict_object_names[subject_id] + ' ' + dict_relation_names[relation_id] + ' ' + dict_object_names[object_id])
                curr_image_graph.append([subject_bbox.tolist(), relation_id, object_bbox.tolist()])

            top_k_predictions.append(curr_predictions)
            top_k_image_graphs.append(curr_image_graph)

        return top_k_predictions, top_k_image_graphs


    def get_unique_top_k_predictions(self, top_k):
        """
        Returns the top k most confident predictions for each image in the format: (subject_id, relation_id, object_id).
        Ensures that one subject-object or object-subject pair appears only once in the predictions.
        """
        top_k_predictions = []
        dict_relation_names = relation_by_super_class_int2str()
        dict_object_names = object_class_int2str()

        for image in torch.unique(self.which_in_batch):  # image-wise
            curr_image = self.which_in_batch == image
            curr_confidence = self.confidence[curr_image]
            sorted_inds = torch.argsort(curr_confidence, dim=0, descending=True)

            curr_predictions = []
            seen_pairs = set()

            for ind in sorted_inds:
                subject_id = self.subject_cat_pred[curr_image][ind].item()
                relation_id = self.relation_pred[curr_image][ind].item()
                object_id = self.object_cat_pred[curr_image][ind].item()

                # Check if the pair (or its reverse) has been added to the predictions
                if (subject_id, object_id) not in seen_pairs and (object_id, subject_id) not in seen_pairs:
                    curr_predictions.append(dict_object_names[subject_id] + ' ' + dict_relation_names[relation_id] + ' ' + dict_object_names[object_id])
                    seen_pairs.add((subject_id, object_id))
                    seen_pairs.add((object_id, subject_id))

                # Stop when we have k predictions
                if len(curr_predictions) == top_k:
                    break

            top_k_predictions.append(curr_predictions)

        return top_k_predictions


    def global_refine(self, relation_pred, confidence, batch_idx, top_k):
        """
        For the batch_idx image in the batch, update the relation_pred and confidence of its top_k predictions.
        Because we calculate the confidence scores in a different way in global graphical refine, we only use new confidence scores
        to reorder the new top_k predictions, without actually
        """
        if not self.hierar:
            # find the top k predictions to be updated
            image = torch.unique(self.which_in_batch)[batch_idx]
            curr_image = self.which_in_batch == image
            curr_confidence = self.confidence[curr_image]
            sorted_inds = torch.argsort(curr_confidence, dim=0, descending=True)

            # select the top k predictions
            this_k = min(top_k, len(self.relation_pred[curr_image]))
            keep_inds = sorted_inds[:this_k]
            # print('keep_inds', keep_inds.shape, keep_inds)
            # print('self.relation_pred[curr_image][keep_inds]', self.relation_pred[curr_image][keep_inds].shape, 'relation_pred', relation_pred.shape)

            # assign new relation predictions
            self.relation_pred[curr_image][keep_inds] = relation_pred

            # shuffle the top k predictions based on their new confidence, without affecting the order of remaining predictions
            reorder_topk_inds = torch.argsort(confidence, descending=True)

            self.relation_pred[curr_image][keep_inds] = self.relation_pred[curr_image][keep_inds][reorder_topk_inds]
            self.relation_target[curr_image][keep_inds] = self.relation_target[curr_image][keep_inds][reorder_topk_inds]
            self.confidence[curr_image][keep_inds] = self.confidence[curr_image][keep_inds][reorder_topk_inds]
            self.connectivity[curr_image][keep_inds] = self.connectivity[curr_image][keep_inds][reorder_topk_inds]

            self.subject_cat_pred[curr_image][keep_inds] = self.subject_cat_pred[curr_image][keep_inds][reorder_topk_inds]
            self.object_cat_pred[curr_image][keep_inds] = self.object_cat_pred[curr_image][keep_inds][reorder_topk_inds]
            self.subject_cat_target[curr_image][keep_inds] = self.subject_cat_target[curr_image][keep_inds][reorder_topk_inds]
            self.object_cat_target[curr_image][keep_inds] = self.object_cat_target[curr_image][keep_inds][reorder_topk_inds]
            self.subject_bbox_pred[curr_image][keep_inds] = self.subject_bbox_pred[curr_image][keep_inds][reorder_topk_inds]
            self.object_bbox_pred[curr_image][keep_inds] = self.object_bbox_pred[curr_image][keep_inds][reorder_topk_inds]
            self.subject_bbox_target[curr_image][keep_inds] = self.subject_bbox_target[curr_image][keep_inds][reorder_topk_inds]
            self.object_bbox_target[curr_image][keep_inds] = self.object_bbox_target[curr_image][keep_inds][reorder_topk_inds]

        else:
            assert False, "Not Implemented"

    # def global_refine(self, refined_relation, connected_indices_accumulated):
    #     if not self.hierar:  # flat relationship prediction
    #         self.relation_pred[connected_indices_accumulated] = torch.argmax(refined_relation, dim=1)
    #         self.confidence[connected_indices_accumulated] = torch.max(refined_relation, dim=1)[0]
    #     else:
    #         connected_indices_accumulated = connected_indices_accumulated.repeat(3)
    #         relation_pred = torch.hstack((torch.argmax(refined_relation[:, :self.args['models']['num_geometric']], dim=1),
    #                                          torch.argmax(refined_relation[:, self.args['models']['num_geometric']:self.args['models']['num_geometric'] + self.args['models']['num_possessive']], dim=1)
    #                                          + self.args['models']['num_geometric'],
    #                                          torch.argmax(refined_relation[:, self.args['models']['num_geometric'] + self.args['models']['num_possessive']:], dim=1)
    #                                          + self.args['models']['num_geometric'] + self.args['models']['num_possessive']))
    #         self.relation_pred[connected_indices_accumulated] = relation_pred
    #
    #         confidence = torch.hstack((torch.max(refined_relation[:, :self.args['models']['num_geometric']], dim=1)[0],
    #                                    torch.max(refined_relation[:, self.args['models']['num_geometric']: self.args['models']['num_geometric'] + self.args['models']['num_possessive']], dim=1)[0],
    #                                    torch.max(refined_relation[:, self.args['models']['num_geometric'] + self.args['models']['num_possessive']:], dim=1)[0]))
    #         self.confidence[connected_indices_accumulated] = confidence


    def compute(self, per_class=False):
        """
        A ground truth predicate is considered to match a hypothesized relationship iff the predicted relationship is correct,
        the subject and object labels match, and the bounding boxes associated with the subject and object both have IOU>0.5 with the ground-truth boxes.
        """
        recall_k_zs, recall_k_per_class_zs, mean_recall_k_zs = None, None, None
        self.confidence += self.connectivity

        for image in torch.unique(self.which_in_batch):  # image-wise
            curr_image = self.which_in_batch == image
            num_relation_pred = len(self.relation_pred[curr_image])
            curr_confidence = self.confidence[curr_image]
            sorted_inds = torch.argsort(curr_confidence, dim=0, descending=True)

            for i in range(len(self.relation_target[curr_image])):
                if self.relation_target[curr_image][i] == -1:  # if target is not connected
                    continue
                if self.args['dataset']['dataset'] == 'vg':
                    curr_triplet = str(self.subject_cat_target[curr_image][i].item()) + '_' + str(self.relation_target[curr_image][i].item()) \
                                   + '_' + str(self.object_cat_target[curr_image][i].item())

                # search in top k most confident predictions in each image
                num_target = torch.sum(self.relation_target[curr_image] != -1)
                this_k = min(self.top_k[-1], num_relation_pred)  # 100
                keep_inds = sorted_inds[:this_k]

                found = False   # found if any one of the three sub-models predict correctly
                for j in range(len(keep_inds)):     # for each target <subject, relation, object> triple, find any match in the top k confident predictions
                    if (self.subject_cat_target[curr_image][i] == self.subject_cat_pred[curr_image][keep_inds][j]
                            and self.object_cat_target[curr_image][i] == self.object_cat_pred[curr_image][keep_inds][j]):

                        sub_iou = self.iou(self.subject_bbox_target[curr_image][i], self.subject_bbox_pred[curr_image][keep_inds][j])
                        obj_iou = self.iou(self.object_bbox_target[curr_image][i], self.object_bbox_pred[curr_image][keep_inds][j])

                        if sub_iou >= self.iou_thresh and obj_iou >= self.iou_thresh:
                            if self.relation_target[curr_image][i] == self.relation_pred[curr_image][keep_inds][j]:
                                for k in self.top_k:
                                    if j >= k:
                                        continue
                                    self.result_dict[k] += 1.0
                                    if per_class:
                                        self.result_per_class[k][self.relation_target[curr_image][i]] += 1.0

                                    # if zero shot
                                    if self.args['dataset']['dataset'] == 'vg':
                                        if curr_triplet in self.zero_shot_triplets:
                                            assert curr_triplet not in self.train_triplets
                                            self.result_dict_zs[k] += 1.0
                                            if per_class:
                                                self.result_per_class_zs[k][self.relation_target[curr_image][i]] += 1.0
                                found = True
                            if found:
                                break

                self.num_connected_target += 1.0
                self.num_conn_target_per_class[self.relation_target[curr_image][i]] += 1.0
                # if zero shot
                if self.args['dataset']['dataset'] == 'vg':
                    if curr_triplet in self.zero_shot_triplets:
                        self.num_connected_target_zs += 1.0
                        self.num_conn_target_per_class_zs[self.relation_target[curr_image][i]] += 1.0

        recall_k = [self.result_dict[k] / max(self.num_connected_target, 1e-3) for k in self.top_k]
        recall_k_per_class = [self.result_per_class[k] / self.num_conn_target_per_class for k in self.top_k]
        mean_recall_k = [torch.nanmean(r) for r in recall_k_per_class]

        if self.args['dataset']['dataset'] == 'vg':
            recall_k_zs = [self.result_dict_zs[k] / max(self.num_connected_target_zs, 1e-3) for k in self.top_k]
            recall_k_per_class_zs = [self.result_per_class_zs[k] / self.num_conn_target_per_class_zs for k in self.top_k]
            mean_recall_k_zs = [torch.nanmean(r) for r in recall_k_per_class_zs]

        return recall_k, recall_k_per_class, mean_recall_k, recall_k_zs, recall_k_per_class_zs, mean_recall_k_zs

    def compute_precision(self):
        for image in torch.unique(self.which_in_batch):  # image-wise
            curr_image = self.which_in_batch == image
            num_relation_pred = len(self.relation_pred[curr_image])
            curr_confidence = self.confidence[curr_image]
            sorted_inds = torch.argsort(curr_confidence, dim=0, descending=True)
            this_k = min(20, num_relation_pred)  # 100
            keep_inds = sorted_inds[:this_k]

            for i in range(len(self.relation_pred[curr_image][keep_inds])):
                found = False  # found if any one of the three sub-models predict correctly
                found_union = False
                for j in range(len(self.relation_target[curr_image])):
                    if self.relation_target[curr_image][j] == -1:  # if target is not connected
                        continue

                    if (self.subject_cat_pred[curr_image][keep_inds][i] == self.subject_cat_target[curr_image][j]
                            and self.object_cat_pred[curr_image][keep_inds][i] == self.object_cat_target[curr_image][j]):

                        sub_iou = self.iou(self.subject_bbox_pred[curr_image][keep_inds][i], self.subject_bbox_target[curr_image][j])
                        obj_iou = self.iou(self.object_bbox_pred[curr_image][keep_inds][i], self.object_bbox_target[curr_image][j])
                        union_iou = self.iou_union(self.subject_bbox_pred[curr_image][keep_inds][i], self.object_bbox_pred[curr_image][keep_inds][i],
                                                   self.subject_bbox_target[curr_image][j], self.object_bbox_target[curr_image][j])

                        if self.relation_pred[curr_image][keep_inds][i] == self.relation_target[curr_image][j]:
                            if sub_iou >= self.iou_thresh and obj_iou >= self.iou_thresh and found == False:
                                self.result_per_class_ap[self.relation_pred[curr_image][keep_inds][i]] += 1.0
                                found = True
                            if union_iou >= self.iou_thresh and found_union == False:
                                self.result_per_class_ap_union[self.relation_pred[curr_image][keep_inds][i]] += 1.0
                                found_union = True

                        if found and found_union:
                            break

                self.num_conn_target_per_class_ap[self.relation_pred[curr_image][keep_inds][i]] += 1.0

        weight = get_weight_oiv6()
        precision_per_class = self.result_per_class_ap / self.num_conn_target_per_class_ap
        not_nan = torch.logical_not(torch.isnan(precision_per_class))
        weighted_mean_precision = torch.nansum(precision_per_class * weight) / torch.sum(weight[not_nan])

        precision_per_class_union = self.result_per_class_ap_union / self.num_conn_target_per_class_ap
        weighted_mean_precision_union = torch.nansum(precision_per_class_union * weight) / torch.sum(weight[not_nan])
        return weighted_mean_precision, weighted_mean_precision_union

    def clear_data(self):
        self.which_in_batch = None
        self.confidence = None
        self.connectivity = None
        self.relation_pred = None
        self.relation_target = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None


class Evaluator_PC_Top3:
    """
    The class evaluate the model performance on Recall@k^{*} and mean Recall@k^{*} evaluation metrics on predicate classification tasks.
    If any of the three super-category output heads correctly predicts the relationship, we score it as a match.
    Top3 represents three argmax predicate from three disjoint super-categories, instead of the top 3 predicates under a flat classification.
    """
    def __init__(self, args, num_classes, iou_thresh, top_k):
        self.args = args
        self.top_k = top_k
        self.num_classes = num_classes
        self.iou_thresh = iou_thresh
        self.num_connected_target = 0.0
        self.motif_total = 0.0
        self.motif_correct = 0.0
        self.result_dict = {20: 0.0, 50: 0.0, 100: 0.0}
        self.result_dict_top1 = {20: 0.0, 50: 0.0, 100: 0.0}
        self.result_per_class = {k: torch.tensor([0.0 for i in range(self.num_classes)]) for k in self.top_k}
        self.result_per_class_top1 = {k: torch.tensor([0.0 for i in range(self.num_classes)]) for k in self.top_k}
        self.num_conn_target_per_class = torch.tensor([0.0 for i in range(self.num_classes)])

        self.which_in_batch = None
        self.confidence = None
        self.connectivity = None
        self.relation_pred = None
        self.relation_target = None
        self.super_relation_pred = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None

    def iou(self, bbox_target, bbox_pred):
        mask_pred = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_pred[int(bbox_pred[2]):int(bbox_pred[3]), int(bbox_pred[0]):int(bbox_pred[1])] = 1
        mask_target = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_target[int(bbox_target[2]):int(bbox_target[3]), int(bbox_target[0]):int(bbox_target[1])] = 1
        intersect = torch.sum(torch.logical_and(mask_target, mask_pred))
        union = torch.sum(torch.logical_or(mask_target, mask_pred))
        if union == 0:
            return 0
        else:
            return float(intersect) / float(union)

    def accumulate(self, which_in_batch, relation_pred, relation_target, super_relation_pred, connectivity,
                   subject_cat_pred, object_cat_pred, subject_cat_target, object_cat_target,
                   subject_bbox_pred, object_bbox_pred, subject_bbox_target, object_bbox_target):  # size (batch_size, num_relations_classes), (num_relations_classes)

        if self.relation_pred is None:
            self.which_in_batch = which_in_batch
            self.connectivity = connectivity
            self.confidence = torch.max(torch.vstack((torch.max(relation_pred[:, :self.args['models']['num_geometric']], dim=1)[0],
                                                      torch.max(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1)[0],
                                                      torch.max(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1)[0])), dim=0)[0]  # in log space, [0] to take values
            self.relation_pred = relation_pred
            self.relation_target = relation_target
            self.super_relation_pred = super_relation_pred

            self.subject_cat_pred = subject_cat_pred
            self.object_cat_pred = object_cat_pred
            self.subject_cat_target = subject_cat_target
            self.object_cat_target = object_cat_target

            self.subject_bbox_pred = subject_bbox_pred
            self.object_bbox_pred = object_bbox_pred
            self.subject_bbox_target = subject_bbox_target
            self.object_bbox_target = object_bbox_target
        else:
            self.which_in_batch = torch.hstack((self.which_in_batch, which_in_batch))
            self.connectivity = torch.hstack((self.connectivity, connectivity))

            confidence = torch.max(torch.vstack((torch.max(relation_pred[:, :self.args['models']['num_geometric']], dim=1)[0],
                                                 torch.max(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1)[0],
                                                 torch.max(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1)[0])), dim=0)[0]  # in log space, [0] to take values
            self.confidence = torch.hstack((self.confidence, confidence))

            self.relation_pred = torch.vstack((self.relation_pred, relation_pred))
            self.relation_target = torch.hstack((self.relation_target, relation_target))
            self.super_relation_pred = torch.vstack((self.super_relation_pred, super_relation_pred))

            self.subject_cat_pred = torch.hstack((self.subject_cat_pred, subject_cat_pred))
            self.object_cat_pred = torch.hstack((self.object_cat_pred, object_cat_pred))
            self.subject_cat_target = torch.hstack((self.subject_cat_target, subject_cat_target))
            self.object_cat_target = torch.hstack((self.object_cat_target, object_cat_target))

            self.subject_bbox_pred = torch.vstack((self.subject_bbox_pred, subject_bbox_pred))
            self.object_bbox_pred = torch.vstack((self.object_bbox_pred, object_bbox_pred))
            self.subject_bbox_target = torch.vstack((self.subject_bbox_target, subject_bbox_target))
            self.object_bbox_target = torch.vstack((self.object_bbox_target, object_bbox_target))

    def global_refine(self, refined_relation, connected_indices_accumulated):
        # print('self.relation_pred', self.relation_pred.shape, 'connected_indices_accumulated', connected_indices_accumulated.shape)
        # print('self.relation_pred[connected_indices_accumulated]', self.relation_pred[connected_indices_accumulated].shape, 'refined_relation', refined_relation.shape)
        self.relation_pred[connected_indices_accumulated, :] = refined_relation

        confidence = torch.max(torch.vstack((torch.max(refined_relation[:, :self.args['models']['num_geometric']], dim=1)[0],
                                             torch.max(refined_relation[:, self.args['models']['num_geometric']:self.args['models']['num_geometric'] + self.args['models']['num_possessive']], dim=1)[0],
                                             torch.max(refined_relation[:, self.args['models']['num_geometric'] + self.args['models']['num_possessive']:], dim=1)[0])), dim=0)[0]
        self.confidence[connected_indices_accumulated] = confidence

    def compute(self, per_class=False):
        """
        A ground truth predicate is considered to match a hypothesized relationship iff the predicted relationship is correct,
        the subject and object labels match, and the bounding boxes associated with the subject and object both have IOU>0.5 with the ground-truth boxes.
        """
        self.confidence += self.connectivity

        for image in torch.unique(self.which_in_batch):  # image-wise
            curr_image = self.which_in_batch == image
            num_relation_pred = len(self.relation_pred[curr_image])
            curr_confidence = self.confidence[curr_image]

            sorted_inds = torch.argsort(curr_confidence, dim=0, descending=True)

            for i in range(len(self.relation_target[curr_image])):
                if self.relation_target[curr_image][i] == -1:  # if target is not connected
                    continue

                # search in top k most confident predictions in each image
                num_target = torch.sum(self.relation_target[curr_image] != -1)
                this_k = min(self.top_k[-1], num_relation_pred)  # 100
                keep_inds = sorted_inds[:this_k]

                found = False   # found if any one of the three sub-models predict correctly
                found_top1 = False  # found if only the most confident one of the three sub-models predict correctly
                for j in range(len(keep_inds)):     # for each target <subject, relation, object> triple, find any match in the top k confident predictions
                    if (self.subject_cat_target[curr_image][i] == self.subject_cat_pred[curr_image][keep_inds][j]
                            and self.object_cat_target[curr_image][i] == self.object_cat_pred[curr_image][keep_inds][j]):

                        sub_iou = self.iou(self.subject_bbox_target[curr_image][i], self.subject_bbox_pred[curr_image][keep_inds][j])
                        obj_iou = self.iou(self.object_bbox_target[curr_image][i], self.object_bbox_pred[curr_image][keep_inds][j])

                        if sub_iou >= self.iou_thresh and obj_iou >= self.iou_thresh:
                            if not found:
                                relation_pred_1 = self.relation_pred[curr_image][keep_inds][j][:self.args['models']['num_geometric']]  # geometric
                                relation_pred_2 = self.relation_pred[curr_image][keep_inds][j][self.args['models']['num_geometric']:self.args['models']['num_geometric']
                                                                                                                                    + self.args['models']['num_possessive']]  # possessive
                                relation_pred_3 = self.relation_pred[curr_image][keep_inds][j][self.args['models']['num_geometric'] + self.args['models']['num_possessive']:]  # semantic
                                if self.relation_target[curr_image][i] == torch.argmax(relation_pred_1) \
                                        or self.relation_target[curr_image][i] == torch.argmax(relation_pred_2) + self.args['models']['num_geometric'] \
                                        or self.relation_target[curr_image][i] == torch.argmax(relation_pred_3) + self.args['models']['num_geometric'] + self.args['models']['num_possessive']:
                                    for k in self.top_k:
                                        if j >= max(k, num_target):
                                            continue
                                        self.result_dict[k] += 1.0
                                        if per_class:
                                            self.result_per_class[k][self.relation_target[curr_image][i]] += 1.0
                                    found = True

                            if not found_top1:
                                curr_super = torch.argmax(self.super_relation_pred[curr_image][keep_inds][j])
                                relation_preds = [torch.argmax(self.relation_pred[curr_image][keep_inds][j][:self.args['models']['num_geometric']]),
                                                  torch.argmax(self.relation_pred[curr_image][keep_inds][j][self.args['models']['num_geometric']:self.args['models']['num_geometric']
                                                                                                            + self.args['models']['num_possessive']]) + self.args['models']['num_geometric'],
                                                  torch.argmax(self.relation_pred[curr_image][keep_inds][j][self.args['models']['num_geometric'] + self.args['models']['num_possessive']:])
                                                                                                            + self.args['models']['num_geometric'] + self.args['models']['num_possessive']]
                                if self.relation_target[curr_image][i] == relation_preds[curr_super]:
                                    for k in self.top_k:
                                        if j >= max(k, num_target):
                                            continue
                                        self.result_dict_top1[k] += 1.0
                                        if per_class:
                                            self.result_per_class_top1[k][self.relation_target[curr_image][i]] += 1.0
                                    found_top1 = True

                            if found and found_top1:
                                break

                self.num_connected_target += 1.0
                self.num_conn_target_per_class[self.relation_target[curr_image][i]] += 1.0

        recall_k = [self.result_dict[k] / max(self.num_connected_target, 1e-3) for k in self.top_k]
        recall_k_per_class = [self.result_per_class[k] / self.num_conn_target_per_class for k in self.top_k]
        mean_recall_k = [torch.nanmean(r) for r in recall_k_per_class]
        # recall_k_top1 = [self.result_dict_top1[k] / self.num_connected_target for k in self.top_k]
        # mean_recall_k_top1 = [torch.nanmean(r) for r in recall_k_per_class_top1]
        return recall_k, recall_k_per_class, mean_recall_k

    def clear_data(self):
        self.which_in_batch = None
        self.confidence = None
        self.relation_pred = None
        self.connectivity = None
        self.relation_target = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None


class Evaluator_PC_NoGraphConstraint:
    """
    The class evaluate the model performance on Recall@k and mean Recall@k evaluation metrics on predicate classification tasks.
    In the no graph constraint scheme, all the 50 predicates will be involved in the recall ranking not just the one with the highest score
    for each edge, and also without the knowledge of relationship hierarchy
    """
    def __init__(self, args, num_classes, iou_thresh, top_k):
        self.args = args
        self.top_k = top_k
        self.num_classes = num_classes
        self.iou_thresh = iou_thresh
        self.num_connected_target = 0.0
        self.motif_total = 0.0
        self.motif_correct = 0.0
        self.result_dict = {20: 0.0, 50: 0.0, 100: 0.0}
        self.result_per_class = {k: torch.tensor([0.0 for i in range(self.num_classes)]) for k in self.top_k}
        self.num_conn_target_per_class = torch.tensor([0.0 for i in range(self.num_classes)])
        self.num_relations = self.args['models']['num_relations']

        self.which_in_batch = None
        self.connected_pred = None
        self.confidence = None
        self.relation_pred = None
        self.relation_target = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None

    def iou(self, bbox_target, bbox_pred):
        mask_pred = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_pred[int(bbox_pred[2]):int(bbox_pred[3]), int(bbox_pred[0]):int(bbox_pred[1])] = 1
        mask_target = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_target[int(bbox_target[2]):int(bbox_target[3]), int(bbox_target[0]):int(bbox_target[1])] = 1
        intersect = torch.sum(torch.logical_and(mask_target, mask_pred))
        union = torch.sum(torch.logical_or(mask_target, mask_pred))
        if union == 0:
            return 0
        else:
            return float(intersect) / float(union)

    def accumulate(self, which_in_batch, relation_pred, relation_target, super_relation_pred, connectivity,
                   subject_cat_pred, object_cat_pred, subject_cat_target, object_cat_target,
                   subject_bbox_pred, object_bbox_pred, subject_bbox_target, object_bbox_target):

        # Create a mask of non-zero elements in relation_pred
        low = torch.sort(relation_pred.view(-1), descending=True)[0]
        low = low[int(0.1*len(low))]
        non_zero_mask = (relation_pred.view(-1) > low).nonzero().squeeze()

        if self.relation_pred is None:
            self.which_in_batch = which_in_batch.repeat_interleave(self.num_relations)[non_zero_mask]
            self.confidence = (connectivity.repeat_interleave(self.num_relations) + relation_pred.view(-1))[non_zero_mask]
            # self.confidence = relation_pred.view(-1)[non_zero_mask]

            self.relation_pred = torch.arange(self.num_relations).repeat(len(relation_pred))[non_zero_mask]
            self.relation_target = relation_target.repeat_interleave(self.num_relations)[non_zero_mask]

            self.subject_cat_pred = subject_cat_pred.repeat_interleave(self.num_relations)[non_zero_mask]
            self.object_cat_pred = object_cat_pred.repeat_interleave(self.num_relations)[non_zero_mask]
            self.subject_cat_target = subject_cat_target.repeat_interleave(self.num_relations)[non_zero_mask]
            self.object_cat_target = object_cat_target.repeat_interleave(self.num_relations)[non_zero_mask]

            self.subject_bbox_pred = subject_bbox_pred.repeat_interleave(self.num_relations, dim=0)[non_zero_mask]
            self.object_bbox_pred = object_bbox_pred.repeat_interleave(self.num_relations, dim=0)[non_zero_mask]
            self.subject_bbox_target = subject_bbox_target.repeat_interleave(self.num_relations, dim=0)[non_zero_mask]
            self.object_bbox_target = object_bbox_target.repeat_interleave(self.num_relations, dim=0)[non_zero_mask]
        else:
            self.which_in_batch = torch.hstack((self.which_in_batch, which_in_batch.repeat_interleave(self.num_relations)[non_zero_mask]))
            self.confidence = torch.hstack((self.confidence, (connectivity.repeat_interleave(self.num_relations) + relation_pred.view(-1))[non_zero_mask]))
            # self.confidence = torch.hstack((self.confidence, relation_pred.view(-1)[non_zero_mask]))

            self.relation_pred = torch.hstack((self.relation_pred, torch.arange(self.num_relations).repeat(len(relation_pred))[non_zero_mask]))
            self.relation_target = torch.hstack((self.relation_target, relation_target.repeat_interleave(self.num_relations)[non_zero_mask]))

            self.subject_cat_pred = torch.hstack((self.subject_cat_pred, subject_cat_pred.repeat_interleave(self.num_relations)[non_zero_mask]))
            self.object_cat_pred = torch.hstack((self.object_cat_pred, object_cat_pred.repeat_interleave(self.num_relations)[non_zero_mask]))
            self.subject_cat_target = torch.hstack((self.subject_cat_target, subject_cat_target.repeat_interleave(self.num_relations)[non_zero_mask]))
            self.object_cat_target = torch.hstack((self.object_cat_target, object_cat_target.repeat_interleave(self.num_relations)[non_zero_mask]))

            self.subject_bbox_pred = torch.vstack((self.subject_bbox_pred, subject_bbox_pred.repeat_interleave(self.num_relations, dim=0)[non_zero_mask]))
            self.object_bbox_pred = torch.vstack((self.object_bbox_pred, object_bbox_pred.repeat_interleave(self.num_relations, dim=0)[non_zero_mask]))
            self.subject_bbox_target = torch.vstack((self.subject_bbox_target, subject_bbox_target.repeat_interleave(self.num_relations, dim=0)[non_zero_mask]))
            self.object_bbox_target = torch.vstack((self.object_bbox_target, object_bbox_target.repeat_interleave(self.num_relations, dim=0)[non_zero_mask]))

    def compute(self, per_class=False):
        """
        A ground truth predicate is considered to match a hypothesized relationship iff the predicted relationship is correct,
        the subject and object labels match, and the bounding boxes associated with the subject and object both have IOU>0.5 with the ground-truth boxes.
        """
        for image in torch.unique(self.which_in_batch):  # image-wise
            curr_image = self.which_in_batch == image
            num_relation_pred = len(self.relation_pred[curr_image])
            curr_confidence = self.confidence[curr_image]
            sorted_inds = torch.argsort(curr_confidence, dim=0, descending=True)

            for i in range(len(self.relation_target[curr_image])):
                if self.relation_target[curr_image][i] == -1:  # if target is not connected
                    continue

                # search in top k most confident predictions in each image
                num_target = torch.sum(self.relation_target[curr_image] != -1)
                this_k = min(self.top_k[-1], num_relation_pred)  # 100
                keep_inds = sorted_inds[:this_k]

                found = False   # found if any one of the three sub-models predict correctly
                for j in range(len(keep_inds)):     # for each target <subject, relation, object> triple, find any match in the top k confident predictions
                    if (self.subject_cat_target[curr_image][i] == self.subject_cat_pred[curr_image][keep_inds][j]
                            and self.object_cat_target[curr_image][i] == self.object_cat_pred[curr_image][keep_inds][j]):

                        sub_iou = self.iou(self.subject_bbox_target[curr_image][i], self.subject_bbox_pred[curr_image][keep_inds][j])
                        obj_iou = self.iou(self.object_bbox_target[curr_image][i], self.object_bbox_pred[curr_image][keep_inds][j])

                        if sub_iou >= self.iou_thresh and obj_iou >= self.iou_thresh:
                            if self.relation_target[curr_image][i] == self.relation_pred[curr_image][keep_inds][j]:
                                for k in self.top_k:
                                    if j >= k:
                                        continue
                                    self.result_dict[k] += 1.0
                                    if per_class:
                                        self.result_per_class[k][self.relation_target[curr_image][i]] += 1.0

                                found = True
                            if found:
                                break

                self.num_connected_target += 1.0
                self.num_conn_target_per_class[self.relation_target[curr_image][i]] += 1.0

        recall_k = [self.result_dict[k] / max(self.num_connected_target, 1e-3) for k in self.top_k]
        recall_k_per_class = [self.result_per_class[k] / self.num_conn_target_per_class for k in self.top_k]
        mean_recall_k = [torch.nanmean(r) for r in recall_k_per_class]

        return recall_k, recall_k_per_class, mean_recall_k

    def clear_data(self):
        self.which_in_batch = None
        self.confidence = None
        self.relation_pred = None
        self.relation_target = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None


class Evaluator_SGD:
    """
    The class evaluate the model performance on Recall@k and mean Recall@k evaluation metrics on scene graph detection tasks.
    In our hierarchical relationship scheme, each edge has three predictions per direction under three disjoint super-categories.
    Therefore, each directed edge outputs three individual candidates to be ranked in the top k most confident predictions instead of one.
    """
    def __init__(self, args, num_classes, iou_thresh, top_k):
        self.args = args
        self.top_k = top_k
        self.num_classes = num_classes
        self.iou_thresh = iou_thresh
        self.num_connected_target = 0.0
        self.motif_total = 0.0
        self.motif_correct = 0.0
        self.result_dict = {20: 0.0, 50: 0.0, 100: 0.0}
        self.result_dict_wrong_label_corr_rel = {20: 0.0, 50: 0.0, 100: 0.0}

        self.result_per_class = {k: torch.tensor([0.0 for i in range(self.num_classes)]) for k in self.top_k}
        self.num_conn_target_per_class = torch.tensor([0.0 for i in range(self.num_classes)])

        # man, person, woman, people, boy, girl, lady, child, kid, men  # tree, plant  # plane, airplane
        self.equiv = [[1, 5, 11, 23, 38, 44, 121, 124, 148, 149], [0, 50], [92, 137]]
        # vehicle -> car, bus, motorcycle, truck, vehicle
        # animal -> zebra, sheep, horse, giraffe, elephant, dog, cow, cat, bird, bear, animal
        # food -> vegetable, pizza, orange, fruit, banana, food
        self.unsymm_equiv = {123: [14, 63, 95, 87, 123], 108: [89, 102, 67, 72, 71, 81, 96, 105, 90, 111, 108], 60: [145, 106, 142, 144, 77, 60]}

        self.which_in_batch = None
        self.connected_pred = None
        self.confidence = None
        self.relation_pred = None
        self.relation_target = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.cat_subject_confidence = None
        self.cat_object_confidence = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None

    def iou(self, bbox_target, bbox_pred):
        mask_pred = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_pred[int(bbox_pred[2]):int(bbox_pred[3]), int(bbox_pred[0]):int(bbox_pred[1])] = 1
        mask_target = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_target[int(bbox_target[2]):int(bbox_target[3]), int(bbox_target[0]):int(bbox_target[1])] = 1
        intersect = torch.sum(torch.logical_and(mask_target, mask_pred))
        union = torch.sum(torch.logical_or(mask_target, mask_pred))
        if union == 0:
            return 0
        else:
            return float(intersect) / float(union)

    def accumulate_pred(self, which_in_batch, relation_pred, super_relation_pred, subject_cat_pred, object_cat_pred, subject_bbox_pred, object_bbox_pred,
                        cat_subject_confidence, cat_object_confidence, connectivity):
        if self.relation_pred is None:
            self.which_in_batch = which_in_batch.repeat(3)

            ins_pair_confidence = cat_subject_confidence + cat_object_confidence
            self.confidence = torch.hstack((torch.max(relation_pred[:, :self.args['models']['num_geometric']], dim=1)[0],
                                            torch.max(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1)[0],
                                            torch.max(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1)[0]))

            self.confidence += connectivity.repeat(3) + ins_pair_confidence.repeat(3)
            self.relation_pred = torch.hstack((torch.argmax(relation_pred[:, :self.args['models']['num_geometric']], dim=1),
                                               torch.argmax(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1) + self.args['models']['num_geometric'],
                                               torch.argmax(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1) + self.args['models']['num_geometric']+self.args['models']['num_possessive']))

            self.subject_cat_pred = subject_cat_pred.repeat(3)
            self.object_cat_pred = object_cat_pred.repeat(3)
            self.subject_bbox_pred = subject_bbox_pred.repeat(3, 1)
            self.object_bbox_pred = object_bbox_pred.repeat(3, 1)

        else:
            self.which_in_batch = torch.hstack((self.which_in_batch, which_in_batch.repeat(3)))

            ins_pair_confidence = cat_subject_confidence + cat_object_confidence
            confidence = torch.hstack((torch.max(relation_pred[:, :self.args['models']['num_geometric']], dim=1)[0],
                                       torch.max(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1)[0],
                                       torch.max(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1)[0]))
            confidence += connectivity.repeat(3) + ins_pair_confidence.repeat(3)
            self.confidence = torch.hstack((self.confidence, confidence))

            relation_pred_candid = torch.hstack((torch.argmax(relation_pred[:, :self.args['models']['num_geometric']], dim=1),
                                                 torch.argmax(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1) + self.args['models']['num_geometric'],
                                                 torch.argmax(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1) + self.args['models']['num_geometric']+self.args['models']['num_possessive']))
            self.relation_pred = torch.hstack((self.relation_pred, relation_pred_candid))

            self.subject_cat_pred = torch.hstack((self.subject_cat_pred, subject_cat_pred.repeat(3)))
            self.object_cat_pred = torch.hstack((self.object_cat_pred, object_cat_pred.repeat(3)))
            self.subject_bbox_pred = torch.vstack((self.subject_bbox_pred, subject_bbox_pred.repeat(3, 1)))
            self.object_bbox_pred = torch.vstack((self.object_bbox_pred, object_bbox_pred.repeat(3, 1)))

    def accumulate_target(self, relation_target, subject_cat_target, object_cat_target, subject_bbox_target, object_bbox_target):
        for i in range(len(relation_target)):
            if relation_target[i] is not None:
                relation_target[i] = relation_target[i].repeat(2)
                subject_cat_target[i] = subject_cat_target[i].repeat(2)
                object_cat_target[i] = object_cat_target[i].repeat(2)
                subject_bbox_target[i] = subject_bbox_target[i].repeat(2, 1)
                object_bbox_target[i] = object_bbox_target[i].repeat(2, 1)

        self.relation_target = relation_target
        self.subject_cat_target = subject_cat_target
        self.object_cat_target = object_cat_target
        self.subject_bbox_target = subject_bbox_target
        self.object_bbox_target = object_bbox_target

    def compare_object_cat(self, pred_cat, target_cat):
        if pred_cat == target_cat:
            return True
        for group in self.equiv:
            if pred_cat in group and target_cat in group:
                return True
        for key in self.unsymm_equiv:
            if pred_cat == key and target_cat in self.unsymm_equiv[key]:
                return True
            elif target_cat == key and pred_cat in self.unsymm_equiv[key]:
                return True
        return False

    def compute(self, per_class=False):
        """
        All object bounding boxes and labels are predicted instead of the ground-truth.
        For each target <subject, relation, object> triplet, find among all top k predicted <subject, relation, object> triplets
        if there is any one with matched subject, relationship, and object categories, and iou>=0.5 for subject and object bounding boxes
        """
        for curr_image in range(len(self.relation_target)):
            if self.relation_target[curr_image] is None:
                continue
            curr_image_pred = self.which_in_batch == curr_image

            for i in range(len(self.relation_target[curr_image])):
                if self.relation_target[curr_image][i] == -1:  # if target is not connected
                    continue
                # search in top k most confident predictions in each image
                num_target = torch.sum(self.relation_target[curr_image] != -1)
                num_relation_pred = len(self.relation_pred[curr_image_pred])

                # # As suggested by Neural Motifs, nearly all annotated relationships are between overlapping boxes
                curr_confidence = self.confidence[curr_image_pred]
                sorted_inds = torch.argsort(curr_confidence, dim=0, descending=True)
                this_k = min(self.top_k[-1], num_relation_pred)  # 100
                keep_inds = sorted_inds[:this_k]

                found = False   # found if any one of the three sub-models predict correctly
                for j in range(len(keep_inds)):     # for each target <subject, relation, object> triple, find any match in the top k confident predictions
                    sub_iou = self.iou(self.subject_bbox_target[curr_image][i], self.subject_bbox_pred[curr_image_pred][keep_inds][j])
                    obj_iou = self.iou(self.object_bbox_target[curr_image][i], self.object_bbox_pred[curr_image_pred][keep_inds][j])
                    if sub_iou >= self.iou_thresh and obj_iou >= self.iou_thresh:

                        if self.relation_target[curr_image][i] == self.relation_pred[curr_image_pred][keep_inds][j]:
                            if (self.compare_object_cat(self.subject_cat_target[curr_image][i], self.subject_cat_pred[curr_image_pred][keep_inds][j]) and
                                    self.compare_object_cat(self.object_cat_target[curr_image][i], self.object_cat_pred[curr_image_pred][keep_inds][j])):
                                for k in self.top_k:
                                    if j >= k:
                                        continue
                                    self.result_dict[k] += 1.0
                                    if per_class:
                                        self.result_per_class[k][self.relation_target[curr_image][i]] += 1.0
                                found = True

                            for k in self.top_k:
                                if j >= k:
                                    continue
                                self.result_dict_wrong_label_corr_rel[k] += 1.0
                            if found:
                                break

                self.num_connected_target += 1.0
                self.num_conn_target_per_class[self.relation_target[curr_image][i]] += 1.0

        recall_k = [self.result_dict[k] / self.num_connected_target for k in self.top_k]
        recall_k_wrong_label_corr_rel = [self.result_dict_wrong_label_corr_rel[k] / self.num_connected_target for k in self.top_k]
        recall_k_per_class = [self.result_per_class[k] / self.num_conn_target_per_class for k in self.top_k]
        mean_recall_k = [torch.nanmean(r) for r in recall_k_per_class]
        return recall_k, recall_k_per_class, mean_recall_k, recall_k_wrong_label_corr_rel

    def clear_data(self):
        self.which_in_batch = None
        self.confidence = None
        self.relation_pred = None
        self.relation_target = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None


class Evaluator_SGD_Top3:
    """
    The class evaluate the model performance on Recall@k^{*} and mean Recall@k^{*} evaluation metrics on scene graph detection tasks.
    If any of the three super-category output heads correctly predicts the relationship, we score it as a match.
    Top3 represents three argmax predicate from three disjoint super-categories, instead of the top 3 predicates under a flat classification.
    """
    def __init__(self, args, num_classes, iou_thresh, top_k):
        self.args = args
        self.top_k = top_k
        self.num_classes = num_classes
        self.iou_thresh = iou_thresh
        self.num_connected_target = 0.0
        self.motif_total = 0.0
        self.motif_correct = 0.0
        self.result_dict = {20: 0.0, 50: 0.0, 100: 0.0}
        self.result_dict_top1 = {20: 0.0, 50: 0.0, 100: 0.0}
        self.result_per_class = {k: torch.tensor([0.0 for i in range(self.num_classes)]) for k in self.top_k}
        self.result_per_class_top1 = {k: torch.tensor([0.0 for i in range(self.num_classes)]) for k in self.top_k}
        self.num_conn_target_per_class = torch.tensor([0.0 for i in range(self.num_classes)])

        # man, person, woman, people, boy, girl, lady, child, kid, men  # tree, plant  # plane, airplane
        self.equiv = [[1, 5, 11, 23, 38, 44, 121, 124, 148, 149], [0, 50], [92, 137]]
        # vehicle -> car, bus, motorcycle, truck, vehicle
        # animal -> zebra, sheep, horse, giraffe, elephant, dog, cow, cat, bird, bear, animal
        # food -> vegetable, pizza, orange, fruit, banana, food
        self.unsymm_equiv = {123: [14, 63, 95, 87, 123], 108: [89, 102, 67, 72, 71, 81, 96, 105, 90, 111, 108], 60: [145, 106, 142, 144, 77, 60]}

        self.which_in_batch = None
        self.connected_pred = None
        self.confidence = None
        self.relation_pred = None
        self.relation_target = None
        self.super_relation_pred = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.cat_subject_confidence = None
        self.cat_object_confidence = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None

    def iou(self, bbox_target, bbox_pred):
        mask_pred = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_pred[int(bbox_pred[2]):int(bbox_pred[3]), int(bbox_pred[0]):int(bbox_pred[1])] = 1
        mask_target = torch.zeros(self.args['models']['feature_size'], self.args['models']['feature_size'])
        mask_target[int(bbox_target[2]):int(bbox_target[3]), int(bbox_target[0]):int(bbox_target[1])] = 1
        intersect = torch.sum(torch.logical_and(mask_target, mask_pred))
        union = torch.sum(torch.logical_or(mask_target, mask_pred))
        if union == 0:
            return 0
        else:
            return float(intersect) / float(union)

    def accumulate_pred(self, which_in_batch, relation_pred, super_relation_pred, subject_cat_pred, object_cat_pred, subject_bbox_pred, object_bbox_pred,
                        cat_subject_confidence, cat_object_confidence, connectivity):
        if self.relation_pred is None:
            self.which_in_batch = which_in_batch
            self.relation_pred = relation_pred
            self.super_relation_pred = super_relation_pred

            self.subject_cat_pred = subject_cat_pred
            self.object_cat_pred = object_cat_pred
            self.subject_bbox_pred = subject_bbox_pred
            self.object_bbox_pred = object_bbox_pred

            ins_pair_confidence = cat_subject_confidence + cat_object_confidence
            self.confidence = connectivity + ins_pair_confidence + torch.max(torch.vstack((torch.max(relation_pred[:, :self.args['models']['num_geometric']], dim=1)[0],
                                                                                           torch.max(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1)[0],
                                                                                           torch.max(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1)[0])), dim=0)[0]  # values
        else:
            self.which_in_batch = torch.hstack((self.which_in_batch, which_in_batch))
            self.relation_pred = torch.vstack((self.relation_pred, relation_pred))
            self.super_relation_pred = torch.vstack((self.super_relation_pred, super_relation_pred))
            self.subject_cat_pred = torch.hstack((self.subject_cat_pred, subject_cat_pred))
            self.object_cat_pred = torch.hstack((self.object_cat_pred, object_cat_pred))
            self.subject_bbox_pred = torch.vstack((self.subject_bbox_pred, subject_bbox_pred))
            self.object_bbox_pred = torch.vstack((self.object_bbox_pred, object_bbox_pred))

            ins_pair_confidence = cat_subject_confidence + cat_object_confidence
            confidence = connectivity + ins_pair_confidence + torch.max(torch.vstack((torch.max(relation_pred[:, :self.args['models']['num_geometric']], dim=1)[0],
                                                                                      torch.max(relation_pred[:, self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']], dim=1)[0],
                                                                                      torch.max(relation_pred[:, self.args['models']['num_geometric']+self.args['models']['num_possessive']:], dim=1)[0])), dim=0)[0]  # values
            self.confidence = torch.hstack((self.confidence, confidence))

    def accumulate_target(self, relation_target, subject_cat_target, object_cat_target, subject_bbox_target, object_bbox_target):
        self.relation_target = relation_target
        self.subject_cat_target = subject_cat_target
        self.object_cat_target = object_cat_target
        self.subject_bbox_target = subject_bbox_target
        self.object_bbox_target = object_bbox_target

    def compare_object_cat(self, pred_cat, target_cat):
        if pred_cat == target_cat:
            return True
        for group in self.equiv:
            if pred_cat in group and target_cat in group:
                return True
        for key in self.unsymm_equiv:
            if pred_cat == key and target_cat in self.unsymm_equiv[key]:
                return True
            elif target_cat == key and pred_cat in self.unsymm_equiv[key]:
                return True
        return False

    def compute(self, per_class=False):
        """
        All object bounding boxes and labels are predicted instead of the ground-truth.
        For each target <subject, relation, object> triplet, find among all top k predicted <subject, relation, object> triplets
        if there is any one with matched subject, relationship, and object categories, and iou>=0.5 for subject and object bounding boxes
        """
        for curr_image in range(len(self.relation_target)):
            if self.relation_target[curr_image] is None:
                continue
            curr_image_pred = self.which_in_batch == curr_image

            for i in range(len(self.relation_target[curr_image])):
                if self.relation_target[curr_image][i] == -1:  # if target is not connected
                    continue
                # search in top k most confident predictions in each image
                num_target = torch.sum(self.relation_target[curr_image] != -1)
                num_relation_pred = len(self.relation_pred[curr_image_pred])

                # # As suggested by Neural Motifs, nearly all annotated relationships are between overlapping boxes
                curr_confidence = self.confidence[curr_image_pred]
                sorted_inds = torch.argsort(curr_confidence, dim=0, descending=True)
                this_k = min(self.top_k[-1], num_relation_pred)  # 100
                keep_inds = sorted_inds[:this_k]

                found = False   # found if any one of the three sub-models predict correctly
                found_top1 = False  # found if only the most confident one of the three sub-models predict correctly
                for j in range(len(keep_inds)):     # for each target <subject, relation, object> triple, find any match in the top k confident predictions
                    if (self.compare_object_cat(self.subject_cat_target[curr_image][i], self.subject_cat_pred[curr_image_pred][keep_inds][j]) and
                        self.compare_object_cat(self.object_cat_target[curr_image][i], self.object_cat_pred[curr_image_pred][keep_inds][j])):

                        sub_iou = self.iou(self.subject_bbox_target[curr_image][i], self.subject_bbox_pred[curr_image_pred][keep_inds][j])
                        obj_iou = self.iou(self.object_bbox_target[curr_image][i], self.object_bbox_pred[curr_image_pred][keep_inds][j])
                        if sub_iou >= self.iou_thresh and obj_iou >= self.iou_thresh:
                            for k in self.top_k:
                                if j >= max(k, num_target):     # in few cases, the number of targets is greater than k=20
                                    continue

                            if not found:     # if already found, skip
                                relation_pred_1 = self.relation_pred[curr_image_pred][keep_inds][j][:self.args['models']['num_geometric']]  # geometric
                                relation_pred_2 = self.relation_pred[curr_image_pred][keep_inds][j][self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']]  # possessive
                                relation_pred_3 = self.relation_pred[curr_image_pred][keep_inds][j][self.args['models']['num_geometric']+self.args['models']['num_possessive']:]  # semantic
                                if self.relation_target[curr_image][i] == torch.argmax(relation_pred_1) \
                                        or self.relation_target[curr_image][i] == torch.argmax(relation_pred_2) + self.args['models']['num_geometric'] \
                                        or self.relation_target[curr_image][i] == torch.argmax(relation_pred_3) + self.args['models']['num_geometric']+self.args['models']['num_possessive']:
                                    for k in self.top_k:
                                        if j >= max(k, num_target):
                                            continue
                                        self.result_dict[k] += 1.0
                                        if per_class:
                                            self.result_per_class[k][self.relation_target[curr_image][i]] += 1.0
                                    found = True

                            if not found_top1:
                                curr_super = torch.argmax(self.super_relation_pred[curr_image_pred][keep_inds][j])
                                relation_preds = [torch.argmax(self.relation_pred[curr_image_pred][keep_inds][j][:self.args['models']['num_geometric']]),
                                                  torch.argmax(self.relation_pred[curr_image_pred][keep_inds][j][self.args['models']['num_geometric']:self.args['models']['num_geometric']+self.args['models']['num_possessive']]) + self.args['models']['num_geometric'],
                                                  torch.argmax(self.relation_pred[curr_image_pred][keep_inds][j][self.args['models']['num_geometric']+self.args['models']['num_possessive']:]) + self.args['models']['num_geometric']+self.args['models']['num_possessive']]
                                if self.relation_target[curr_image][i] == relation_preds[curr_super]:
                                    for k in self.top_k:
                                        if j >= max(k, num_target):
                                            continue
                                        self.result_dict_top1[k] += 1.0
                                        if per_class:
                                            self.result_per_class_top1[k][self.relation_target[curr_image][i]] += 1.0
                                    found_top1 = True

                            if found and found_top1:
                                break

                self.num_connected_target += 1.0
                self.num_conn_target_per_class[self.relation_target[curr_image][i]] += 1.0

        recall_k = [self.result_dict[k] / self.num_connected_target for k in self.top_k]
        recall_k_per_class = [self.result_per_class[k] / self.num_conn_target_per_class for k in self.top_k]
        recall_k_per_class_top1 = [self.result_per_class_top1[k] / self.num_conn_target_per_class for k in self.top_k]
        mean_recall_k = [torch.nanmean(r) for r in recall_k_per_class]
        recall_k_top1 = [self.result_dict_top1[k] / self.num_connected_target for k in self.top_k]
        mean_recall_k_top1 = [torch.nanmean(r) for r in recall_k_per_class_top1]
        return recall_k, recall_k_per_class, mean_recall_k

    def clear_data(self):
        self.which_in_batch = None
        self.confidence = None
        self.relation_pred = None
        self.relation_target = None

        self.subject_cat_pred = None
        self.object_cat_pred = None
        self.subject_cat_target = None
        self.object_cat_target = None

        self.subject_bbox_pred = None
        self.object_bbox_pred = None
        self.subject_bbox_target = None
        self.object_bbox_target = None
