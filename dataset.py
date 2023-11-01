import os
import numpy as np
import torch
import json
from PIL import Image
import string
import tqdm
import torchvision
from torchvision import transforms
from collections import Counter
from utils import *
from dataset_utils import *
import cv2
import random
from dataset_utils import TwoCropTransform


class PrepareVisualGenomeDataset(torch.utils.data.Dataset):
    def __init__(self, annotations):
        with open(annotations) as f:
            self.annotations = json.load(f)

    def __getitem__(self, idx):
        return None

    def __len__(self):
        return len(self.annotations['images'])


class VisualGenomeDataset(torch.utils.data.Dataset):
    def __init__(self, args, device, annotations, training):
        self.args = args
        self.device = device
        self.training = True
        self.image_dir = self.args['dataset']['image_dir']
        self.annot_dir = self.args['dataset']['annot_dir']
        self.subset_indices = None
        with open(annotations) as f:
            self.annotations = json.load(f)
        self.image_transform = transforms.Compose([transforms.ToTensor(),
                                                   transforms.Resize(size=600, max_size=1000, antialias=True)])
        self.image_transform_to_tensor = transforms.ToTensor()
        self.image_transform_s = transforms.Compose([transforms.ToTensor(),
                                                     transforms.Resize((self.args['models']['image_size'], self.args['models']['image_size']), antialias=True)])
        self.image_transform_s_jitter = transforms.Compose([transforms.ToTensor(),
                                                            transforms.RandomApply([
                                                                 transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)
                                                            ], p=0.8),
                                                            transforms.Resize((self.args['models']['image_size'], self.args['models']['image_size']), antialias=True)])
        self.image_transform_contrastive = TwoCropTransform(self.image_transform_s, self.image_transform_s_jitter)
        # self.image_norm = transforms.Compose([transforms.Normalize((103.530, 116.280, 123.675), (1.0, 1.0, 1.0))])
        self.image_norm = transforms.Compose([transforms.Normalize((102.9801, 115.9465, 122.7717), (1.0, 1.0, 1.0))])

        if self.args['training']['run_mode'] == 'clip_zs' or self.args['training']['run_mode'] == 'clip_train' or args['training']['run_mode'] == 'clip_eval':
            self.dict_relation_names = relation_by_super_class_int2str()
            self.dict_object_names = object_class_int2str()

        self.mean_num_rel = 0
        self.mean_num_rel_semi = 0
        self.img_count = 0
        self.num_added_rel_semi = 0

    def __getitem__(self, idx):
        """
        Dataloader Outputs:
            image: an image in the Visual Genome dataset (to predict bounding boxes and labels in DETR-101)
            image_depth: the estimated image depth map
            categories: categories of all objects in the image
            super_categories: super-categories of all objects in the image
            masks: squared masks of all objects in the image
            bbox: bounding boxes of all objects in the image
            relationships: all target relationships in the image
            subj_or_obj: the edge directions of all target relationships in the image
        """
        annot_name = self.annotations['images'][idx]['file_name'][:-4] + '_annotations.pkl'
        annot_path = os.path.join(self.annot_dir, annot_name)
        if os.path.exists(annot_path):
            curr_annot = torch.load(annot_path)
        else:
            return None

        if self.args['training']['run_mode'] == 'train_semi' and self.training:     # no pseudo labels at testing time
            annot_name_semi = 'semi_cs/' + self.annotations['images'][idx]['file_name'][:-4] + '_pseudo_annotations.pkl'
            annot_path_semi = os.path.join(self.annot_dir, annot_name_semi)
            if os.path.exists(annot_path_semi):
                curr_annot_semi = torch.load(annot_path_semi)
            else:
                return None

        image_path = os.path.join(self.image_dir, self.annotations['images'][idx]['file_name'])
        image = cv2.imread(image_path)
        width, height = image.shape[0], image.shape[1]

        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = 255 * self.image_transform_contrastive(image)
        image, image_aug = image[0], image[1]
        image = self.image_norm(image)  # squared size that unifies the size of feature maps
        image_aug = self.image_norm(image_aug)
        self.img_count += 1

        if self.args['training']['run_mode'] == 'eval' and self.args['training']['eval_mode'] != 'pc':
            del image_aug
            image_nonsq = Image.open(image_path).convert('RGB')  # keep original shape ratio, not reshaped to square
            image_nonsq = 255 * self.image_transform(image_nonsq)[[2, 1, 0]]  # BGR
            image_nonsq = self.image_norm(image_nonsq)
        elif self.args['training']['run_mode'] == 'clip_zs' or self.args['training']['run_mode'] == 'clip_train' or self.args['training']['run_mode'] == 'clip_eval':
            del image_aug
            if self.args['training']['eval_mode'] != 'pc':
                image_nonsq = Image.open(image_path).convert('RGB')  # keep original shape ratio, not reshaped to square
                image_nonsq = 255 * self.image_transform(image_nonsq)[[2, 1, 0]]  # BGR
                image_nonsq = self.image_norm(image_nonsq)
            image_raw = Image.open(image_path).convert('RGB')
            image_raw = self.image_transform_to_tensor(image_raw)

        if self.args['models']['use_depth']:
            image_depth = curr_annot['image_depth']
        else:
            image_depth = torch.zeros(1, self.args['models']['feature_size'], self.args['models']['feature_size'])    # ablation no depth map
        categories = curr_annot['categories']
        super_categories = curr_annot['super_categories']
        # total in train: 60548, >20: 2651, >30: 209, >40: 23, >50: 4. Don't let rarely long data dominate the computation power.
        if categories.shape[0] <= 1 or categories.shape[0] > 20:
            return None
        bbox = curr_annot['bbox']   # x_min, x_max, y_min, y_max

        bbox_raw = bbox.clone() / self.args['models']['feature_size']
        bbox_raw[:2] *= height
        bbox_raw[2:] *= width
        bbox_raw = bbox_raw.ceil().int()
        if torch.any(bbox_raw[:, 1] - bbox_raw[:, 0] <= 0) or torch.any(bbox_raw[:, 3] - bbox_raw[:, 2] <= 0):
            return None
        bbox = bbox.int()

        subj_or_obj = curr_annot['subj_or_obj']
        relationships = curr_annot['relationships']
        relationships_reordered = []
        rel_reorder_dict = relation_class_freq2scat()
        for rel in relationships:
            rel[rel == 12] = 4      # wearing <- wears
            relationships_reordered.append(rel_reorder_dict[rel])
            self.mean_num_rel += len(rel[rel != -1])
        relationships = relationships_reordered

        if self.args['training']['run_mode'] == 'train_semi' and self.training:
            # print('relationships before', relationships)
            old_num_added_rel_semi = self.num_added_rel_semi
            relationships, subj_or_obj = self.integrate_pseudo_labels(relationships, subj_or_obj, curr_annot_semi, bbox)
            self.mean_num_rel_semi += self.num_added_rel_semi - old_num_added_rel_semi
            # print('relationships after', relationships, '\n')

        if self.args['training']['run_mode'] == 'clip_zs' or self.args['training']['run_mode'] == 'clip_train' or self.args['training']['run_mode'] == 'clip_eval':
            # reformulate relation annots for a single image in a more efficient way
            triplets = []
            for i, (rels, sos) in enumerate(zip(relationships, subj_or_obj)):
                for j, (rel, so) in enumerate(zip(rels, sos)):
                    bbox_sub = bbox_raw[i + 1]
                    bbox_obj = bbox_raw[j]

                    if so == 1:  # if subject
                        triplets.append((tuple(bbox_sub.tolist()), rel.item(), tuple(bbox_obj.tolist()),
                                         self.dict_object_names[categories[i + 1].item()] + ' ' + self.dict_relation_names[rel.item()] + ' ' + self.dict_object_names[categories[j].item()]))
                    elif so == 0:  # if object
                        triplets.append((tuple(bbox_obj.tolist()), rel.item(), tuple(bbox_sub.tolist()),
                                         self.dict_object_names[categories[j].item()] + ' ' + self.dict_relation_names[rel.item()] + ' ' + self.dict_object_names[categories[i + 1].item()]))

        """
        image: the image transformed to a squared shape of size self.args['models']['image_size'] (to generate a uniform-sized image features)
        image_nonsq: the image transformed to a shape of size=600, max_size=1000 (used in SGCLS and SGDET to predict bounding boxes and labels in DETR-101)
        image_aug: the image transformed to a squared shape of size self.args['models']['image_size'] with color jittering (used in contrastive learning only)
        image_raw: the image transformed to tensor retaining its original shape (used in CLIP only)
        """

        if self.args['training']['run_mode'] == 'eval' and self.args['training']['eval_mode'] != 'pc':
            return image, image_nonsq, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, annot_name
        elif self.args['training']['run_mode'] == 'clip_zs' or self.args['training']['run_mode'] == 'clip_train' or self.args['training']['run_mode'] == 'clip_eval':
            if self.args['training']['eval_mode'] == 'pc':
                return image, image_raw, image_depth, categories, super_categories, bbox, height, width, relationships, subj_or_obj, triplets
            else:
                return image, image_nonsq, image_raw, image_depth, categories, super_categories, bbox, height, width, relationships, subj_or_obj, triplets
        else:
            return image, image_aug, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, annot_name

    def calculate_mean_num_rel_before_after_semi(self):
        print('Mean_num_rel', self.mean_num_rel, 'mean_num_rel_semi', self.mean_num_rel_semi, 'img_count', self.img_count)
        mean_num_rel, mean_num_rel_semi = self.mean_num_rel / self.img_count, self.mean_num_rel_semi / self.img_count
        self.mean_num_rel, self.mean_num_rel_semi, self.img_count, self.num_added_rel_semi = 0, 0, 0, 0
        return mean_num_rel, mean_num_rel_semi

    def integrate_pseudo_labels(self, relationships, subj_or_obj, annot_semi, bbox):
        for edge in annot_semi:
            subject_bbox_semi, relation_id, object_bbox_semi = edge

            # Match bbox for subject and object
            subject_bbox_idx = self.match_bbox(subject_bbox_semi, bbox)
            object_bbox_idx = self.match_bbox(object_bbox_semi, bbox)
            if subject_bbox_idx == object_bbox_idx:
                continue

            if subject_bbox_idx is not None and object_bbox_idx is not None:
                # print('edge', edge, 'subject_bbox_idx', subject_bbox_idx, 'object_bbox_idx', object_bbox_idx)
                if subject_bbox_idx < object_bbox_idx:
                    # If subject comes before the object in bbox order, it goes in subj_or_obj as 1
                    if relationships[object_bbox_idx - 1][subject_bbox_idx].item() == -1:  # only assign the pseudo label if no relationship is assigned yet
                        subj_or_obj[object_bbox_idx - 1][subject_bbox_idx] = 0
                        relationships[object_bbox_idx - 1][subject_bbox_idx] = relation_id
                        self.num_added_rel_semi += 1
                else:
                    if relationships[subject_bbox_idx - 1][object_bbox_idx].item() == -1:
                        subj_or_obj[subject_bbox_idx - 1][object_bbox_idx] = 1
                        relationships[subject_bbox_idx - 1][object_bbox_idx] = relation_id
                        self.num_added_rel_semi += 1

        return relationships, subj_or_obj

    def match_bbox(self, bbox_semi, bbox_raw):
        """
        Returns the index of the bounding box from bbox_raw that most closely matches the pseudo bbox.
        """
        if self.args['training']['eval_mode'] == 'pc':
            for idx, bbox in enumerate(bbox_raw):
                if torch.sum(torch.abs(bbox - torch.as_tensor(bbox_semi))) == 0:
                    return idx
            return None
        else:
            ious = self.calculate_iou_for_all(bbox_semi, bbox_raw)
            return torch.argmax(ious).item()

    def calculate_iou_for_all(self, box1, boxes):
        """
        Calculate the Intersection over Union (IoU) of a bounding box with a set of bounding boxes.
        """
        x1 = torch.max(box1[0], boxes[:, 0])
        y1 = torch.max(box1[1], boxes[:, 1])
        x2 = torch.min(box1[2], boxes[:, 2])
        y2 = torch.min(box1[3], boxes[:, 3])

        inter_area = torch.clamp(x2 - x1 + 1, 0) * torch.clamp(y2 - y1 + 1, 0)

        box1_area = (box1[2] - box1[0] + 1) * (box1[3] - box1[1] + 1)
        boxes_area = (boxes[:, 2] - boxes[:, 0] + 1) * (boxes[:, 3] - boxes[:, 1] + 1)

        union_area = box1_area + boxes_area - inter_area

        return inter_area / union_area

    def load_one_image(self, file_name=None, idx=None, return_annot=False):
        # only return the image for inference
        if not return_annot:
            if file_name is not None:
                image_path = file_name
                image = cv2.imread(image_path)
            else:
                image_path = os.path.join(self.image_dir, self.annotations['images'][idx]['file_name'])
                image = cv2.imread(image_path)

            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            image = 255 * self.image_transform_s(image)
            image = self.image_norm(image)
            return image

        # return the image and image annotations
        else:
            if id is not None:
                annot_name = self.annotations['images'][idx]['file_name'][:-4] + '_annotations.pkl'
                annot_path = os.path.join(self.annot_dir, annot_name)
            else:
                annot_path = os.path.join(self.annot_dir, file_name)
            try:
                curr_annot = torch.load(annot_path)
            except:
                return None

            image_path = os.path.join(self.image_dir, self.annotations['images'][idx]['file_name'])
            images = cv2.imread(image_path)

            images = cv2.cvtColor(images, cv2.COLOR_BGR2RGB)
            images = 255 * self.image_transform_s(images)
            images = self.image_norm(images)

            image_depth = curr_annot['image_depth']
            categories = curr_annot['categories']
            super_categories = curr_annot['super_categories']
            if categories.shape[0] <= 1:
                return None
            bbox = curr_annot['bbox']   # x_min, x_max, y_min, y_max

            subj_or_obj = curr_annot['subj_or_obj']
            relationships = curr_annot['relationships']
            relationships_reordered = []
            rel_reorder_dict = relation_class_freq2scat()
            for rel in relationships:
                rel[rel == 12] = 4      # wearing <- wears
                relationships_reordered.append(rel_reorder_dict[rel])
            relationships = relationships_reordered

            # # reformulate relation annots for a single image in a more efficient way
            # triplets = []
            # for i, (rels, sos) in enumerate(zip(relationships, subj_or_obj)):
            #     for j, (rel, so) in enumerate(zip(rels, sos)):
            #         if so == 1:  # if subject
            #             triplets.append([categories[i + 1].item(), rel.item(), categories[j].item()])
            #         elif so == 0:  # if object
            #             triplets.append([categories[j].item(), rel.item(), categories[i + 1].item()])

            # print('categories', categories)
            # print('triplets', triplets)

            return (images,), (image_path,), (image_depth,), (categories,), (super_categories,), (bbox,), (relationships,), (subj_or_obj,)

    def __len__(self):
        return len(self.annotations['images'])


class VisualGenomeDatasetEfficient(torch.utils.data.Dataset):
    def __init__(self, args, device, annotations):
        self.args = args
        self.device = device
        self.image_dir = self.args['dataset']['image_dir']
        self.annot_dir = self.args['dataset']['annot_dir']
        self.subset_indices = None
        with open(annotations) as f:
            self.annotations = json.load(f)
        self.image_transform_to_tensor = transforms.ToTensor()
        self.image_transform = transforms.Compose([transforms.ToTensor(),
                                                     transforms.Resize((self.args['models']['image_size'], self.args['models']['image_size']))])

        self.image_transform_jitter = transforms.Compose([transforms.ToTensor(),
                                                   transforms.RandomApply([
                                                        transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)
                                                   ], p=0.8),
                                                   transforms.Resize((self.args['models']['image_size'], self.args['models']['image_size']))])
        self.image_norm = transforms.Compose([transforms.Normalize((102.9801, 115.9465, 122.7717), (1.0, 1.0, 1.0))])

    def __getitem__(self, idx):
        """
        Dataloader Outputs:
            image: an image in the Visual Genome dataset (to predict bounding boxes and labels in DETR-101)
            image_s: an image in the Visual Genome dataset resized to a square shape (to generate a uniform-sized image features)
            image_depth: the estimated image depth map
            categories: categories of all objects in the image
            super_categories: super-categories of all objects in the image
            masks: squared masks of all objects in the image
            bbox: bounding boxes of all objects in the image
            relationships: all target relationships in the image
            subj_or_obj: the edge directions of all target relationships in the image
        """
        annot_name = self.annotations['images'][idx]['file_name'][:-4] + '_annotations.pkl'
        annot_path = os.path.join(self.annot_dir, annot_name)
        try:
            curr_annot = torch.load(annot_path)
        except:
            return None

        image_path = os.path.join(self.image_dir, self.annotations['images'][idx]['file_name'])

        image = cv2.imread(image_path)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = 255 * self.image_transform(image)
        image = self.image_norm(image)  # original size that produce better bounding boxes

        if self.args['models']['use_depth']:
            image_depth = curr_annot['image_depth']
        else:
            image_depth = torch.zeros(1, self.args['models']['feature_size'], self.args['models']['feature_size'])    # ablation no depth map

        categories = curr_annot['categories']
        super_categories = curr_annot['super_categories']
        masks = curr_annot['masks']
        # total in train: 60548, >20: 2651, >30: 209, >40: 23, >50: 4. Don't let rarely long data dominate the computation power.
        if masks.shape[0] <= 1 or masks.shape[0] > 30: # 25
            return None
        bbox = curr_annot['bbox']   # x_min, x_max, y_min, y_max

        subj_or_obj = curr_annot['subj_or_obj']
        relationships = curr_annot['relationships']
        relationships_reordered = []
        rel_reorder_dict = relation_class_freq2scat()
        for rel in relationships:
            rel[rel == 12] = 4      # wearing <- wears
            relationships_reordered.append(rel_reorder_dict[rel])
        relationships = relationships_reordered

        triplets = []
        for i in range(len(categories)):
            for j in range(i):
                if subj_or_obj[i-1][j] == 1:
                    triplets.append([categories[i], super_categories[i], bbox[i], masks[i], relationships[i-1][j],
                                     categories[j], super_categories[j], bbox[j], masks[j]])
                elif subj_or_obj[i-1][j] == 0:
                    triplets.append([categories[j], super_categories[j], bbox[j], masks[j], relationships[i-1][j],
                                     categories[i], super_categories[i], bbox[i], masks[i]])
                else:
                    continue    # no relationship in the annotation

        if len(triplets) == 0:
            return None
        # print("triplets", triplets, "\n")

        return image, image_depth, triplets

    def __len__(self):
        return len(self.annotations['images'])


class PrepareOpenImageV6Dataset(torch.utils.data.Dataset):
    def __init__(self, args, annotations):
        self.image_dir = "../datasets/open_image_v6/images/"
        self.image_transform = transforms.Compose([transforms.ToTensor(),
                                                   transforms.Resize((args['models']['image_size'], args['models']['image_size']))])
        with open(annotations) as f:
            self.annotations = json.load(f)

    def __getitem__(self, idx):
        rel = self.annotations[idx]['rel']
        image_id = self.annotations[idx]['img_fn'] + '.jpg'
        image_path = os.path.join(self.image_dir, image_id)
        image = Image.open(image_path).convert('RGB')
        image = self.image_transform(image)
        return rel, image, self.annotations[idx]['img_fn']

    def __len__(self):
        return len(self.annotations)


class OpenImageV6Dataset(torch.utils.data.Dataset):
    def __init__(self, args, device, annotations):
        self.args = args
        self.device = device
        self.image_dir = "../datasets/open_image_v6/images/"
        self.depth_dir = "../datasets/open_image_v6/image_depths/"
        with open(annotations) as f:
            self.annotations = json.load(f)
        self.image_transform = transforms.Compose([transforms.ToTensor(),
                                                   transforms.Resize(size=600, max_size=1000)])
        self.image_transform_s = transforms.Compose([transforms.ToTensor(),
                                                     transforms.Resize((self.args['models']['image_size'], self.args['models']['image_size']))])
        self.image_norm = transforms.Compose([transforms.Normalize((103.530, 116.280, 123.675), (1.0, 1.0, 1.0))])
        self.rel_super_dict = oiv6_reorder_by_super()

    def __getitem__(self, idx):
        # print('idx', idx, self.annotations[idx])
        image_id = self.annotations[idx]['img_fn']
        image_path = os.path.join(self.image_dir, image_id + '.jpg')

        image = Image.open(image_path).convert('RGB')
        h_img, w_img = self.annotations[idx]['img_size'][1], self.annotations[idx]['img_size'][0]

        image = 255 * self.image_transform(image)[[2, 1, 0]]  # BGR
        image = self.image_norm(image)  # original size that produce better bounding boxes
        image_s = Image.open(image_path).convert('RGB')
        image_s = 255 * self.image_transform_s(image_s)[[2, 1, 0]]  # BGR
        image_s = self.image_norm(image_s)  # squared size that unifies the size of feature maps

        if self.args['models']['use_depth']:
            image_depth = torch.load(self.depth_dir + image_id + '_depth.pt')
        else:
            image_depth = torch.zeros(1, self.args['models']['feature_size'], self.args['models']['feature_size'])

        categories = torch.tensor(self.annotations[idx]['det_labels'])
        if len(categories) <= 1 or len(categories) > 20: # 25
            return None

        bbox = []
        raw_bbox = self.annotations[idx]['bbox']    # x_min, y_min, x_max, y_max
        masks = torch.zeros(len(raw_bbox), self.args['models']['feature_size'], self.args['models']['feature_size'], dtype=torch.uint8)
        for i, b in enumerate(raw_bbox):
            box = resize_boxes(b, (h_img, w_img), (self.args['models']['feature_size'], self.args['models']['feature_size']))
            masks[i, box[0]:box[2], box[1]:box[3]] = 1
            bbox.append([box[0], box[2], box[1], box[3]])  # x_min, x_max, y_min, y_max
        bbox = torch.as_tensor(bbox)

        raw_relations = self.annotations[idx]['rel']
        relationships = []
        subj_or_obj = []
        for i in range(1, len(categories)):
            relationships.append(-1 * torch.ones(i, dtype=torch.int64))
            subj_or_obj.append(-1 * torch.ones(i, dtype=torch.float32))

        for triplet in raw_relations:
            # if curr is the subject
            if triplet[0] > triplet[1]:
                relationships[triplet[0]-1][triplet[1]] = self.rel_super_dict[triplet[2]]
                subj_or_obj[triplet[0]-1][triplet[1]] = 1
            # if curr is the object
            elif triplet[0] < triplet[1]:
                relationships[triplet[1]-1][triplet[0]] = self.rel_super_dict[triplet[2]]
                subj_or_obj[triplet[1]-1][triplet[0]] = 0

        return image, image_s, image_depth, categories, None, masks, bbox, relationships, subj_or_obj

    def __len__(self):
        return len(self.annotations)
