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
        self.training = training
        self.image_dir = self.args['dataset']['image_dir']
        self.annot_dir = self.args['dataset']['annot_dir']
        self.subset_indices = None
        with open(annotations) as f:
            self.annotations = json.load(f)
        self.image_transform = transforms.Compose([transforms.ToTensor(),
                                                   transforms.Resize(size=600, max_size=1000, antialias=True)])
        self.image_transform_to_tensor = transforms.ToTensor()
        self.image_transform_square = transforms.Compose([transforms.ToTensor(),
                                                     transforms.Resize((self.args['models']['image_size'], self.args['models']['image_size']), antialias=True)])
        self.image_transform_square_jitter = transforms.Compose([transforms.ToTensor(),
                                                            transforms.RandomApply([
                                                                 transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)
                                                            ], p=0.8),
                                                            transforms.Resize((self.args['models']['image_size'], self.args['models']['image_size']), antialias=True)])
        self.image_transform_contrastive = TwoCropTransform(self.image_transform_square, self.image_transform_square_jitter)
        self.image_norm = transforms.Compose([transforms.Normalize((102.9801, 115.9465, 122.7717), (1.0, 1.0, 1.0))])

        self.train_cs_step = 1  # 1 or 2
        self.triplets_train_gt = {}
        self.triplets_train_pseudo = {}
        self.commonsense_aligned_triplets = {}
        self.commonsense_violated_triplets = {}

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

        if self.args['training']['run_mode'] == 'prepare_cs' and self.train_cs_step == 2:
            # load saved commonsense-aligned and violated triplets for each current image
            annot_name_yes = 'cs_aligned_top10/' + self.annotations['images'][idx]['file_name'][:-4] + '_pseudo_annotations.pkl'
            annot_name_yes = os.path.join(self.annot_dir, annot_name_yes)
            if os.path.exists(annot_name_yes):
                curr_annot_yes = torch.load(annot_name_yes)
            else:
                return None
            annot_name_no = 'cs_violated_top10/' + self.annotations['images'][idx]['file_name'][:-4] + '_pseudo_annotations.pkl'
            annot_name_no = os.path.join(self.annot_dir, annot_name_no)
            if os.path.exists(annot_name_no):
                curr_annot_no = torch.load(annot_name_no)
            else:
                return None
            # print(annot_name_yes, annot_name_no)

        image_path = os.path.join(self.image_dir, self.annotations['images'][idx]['file_name'])
        image = cv2.imread(image_path)
        width, height = image.shape[0], image.shape[1]

        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = 255 * self.image_transform_contrastive(image)
        image, image_aug = image[0], image[1]
        image = self.image_norm(image)  # squared size that unifies the size of feature maps
        image_aug = self.image_norm(image_aug)

        if self.args['training']['run_mode'] == 'eval' or self.args['training']['run_mode'] == 'eval_cs':
            del image_aug
            # if self.args['training']['eval_mode'] != 'pc':
            image_nonsq = Image.open(image_path).convert('RGB')  # keep original shape ratio, not reshaped to square
            image_nonsq = 255 * self.image_transform(image_nonsq)[[2, 1, 0]]  # BGR
            image_nonsq = self.image_norm(image_nonsq)

        image_depth = curr_annot['image_depth'] if self.args['models']['use_depth'] \
            else torch.zeros(1, self.args['models']['feature_size'], self.args['models']['feature_size'])

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
        relationships = relationships_reordered

        if self.args['training']['run_mode'] == 'prepare_cs' and self.train_cs_step == 2:
            self.accumulate_triplets(categories, relationships, subj_or_obj, bbox, curr_annot_yes, curr_annot_no)

        """
        image: the image transformed to a squared shape of size self.args['models']['image_size'] (to generate a uniform-sized image features)
        image_nonsq: the image transformed to a shape of size=600, max_size=1000 (used in SGCLS and SGDET to predict bounding boxes and labels in DETR-101)
        image_aug: the image transformed to a squared shape of size self.args['models']['image_size'] with color jittering (used in contrastive learning only)
        image_raw: the image transformed to tensor retaining its original shape (used in CLIP only)
        """

        if self.args['training']['run_mode'] == 'eval' or self.args['training']['run_mode'] == 'eval_cs':
            if self.args['training']['save_vis_results'] and self.args['training']['eval_mode'] == 'pc':
                return image, image_nonsq, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, annot_name, height, width, triplets, bbox_raw
            else:
                return image, image_nonsq, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, annot_name
        else:
            return image, image_aug, image_depth, categories, super_categories, bbox, relationships, subj_or_obj, annot_name


    def accumulate_triplets(self, categories, relationships, subj_or_obj, bbox, annot_name_yes, annot_name_no):
        """
        This function is called iff run_mode == 'prepare_cs' and train_cs_step == 2
        It accumulates all the commonsense-aligned and violated triplets for each current image
        by gradually adding triplets into two dictionaries named self.commonsense_aligned_triplets and self.commonsense_violated_triplets during the inference
        """
        for i, (rels, sos) in enumerate(zip(relationships, subj_or_obj)):
            for j, (rel, so) in enumerate(zip(rels, sos)):
                if so == 1:  # if subject
                    key = (categories[i + 1].item(), rel.item(), categories[j].item())
                elif so == 0:  # if object
                    key = (categories[j].item(), rel.item(), categories[i + 1].item())
                else:
                    continue

                # check if the key is already in the dictionary, if not, initialize the count to 0
                if key not in self.triplets_train_gt:
                    self.triplets_train_gt[key] = 0
                self.triplets_train_gt[key] += 1

        for edge in annot_name_yes:
            subject_bbox, relation_id, object_bbox, _, _ = edge

            # Match bbox for subject and object
            subject_idx = match_bbox(subject_bbox, bbox, self.args['training']['eval_mode'])
            object_idx = match_bbox(object_bbox, bbox, self.args['training']['eval_mode'])
            if subject_idx == object_idx:
                continue

            if subject_idx is not None and object_idx is not None:
                key = (categories[subject_idx].item(), relation_id, categories[object_idx].item())
                # check if the key is already in the dictionary, if not, initialize the count to 0
                if key not in self.commonsense_aligned_triplets:
                    self.commonsense_aligned_triplets[key] = 0
                self.commonsense_aligned_triplets[key] += 1

        for edge in annot_name_no:
            subject_bbox, relation_id, object_bbox, _, _ = edge

            # Match bbox for subject and object
            subject_idx = match_bbox(subject_bbox, bbox, self.args['training']['eval_mode'])
            object_idx = match_bbox(object_bbox, bbox, self.args['training']['eval_mode'])
            if subject_idx == object_idx:
                continue

            if subject_idx is not None and object_idx is not None:
                key = (categories[subject_idx].item(), relation_id, categories[object_idx].item())
                # check if the key is already in the dictionary, if not, initialize the count to 0
                if key not in self.commonsense_violated_triplets:
                    self.commonsense_violated_triplets[key] = 0
                self.commonsense_violated_triplets[key] += 1


    def save_all_triplets(self):
        """
        This function is called iff run_mode == 'prepare_cs' and train_cs_step == 2
        It saves the two dictionaries named self.commonsense_aligned_triplets and self.commonsense_violated_triplets as two .pt files at the end of the inference
        """
        # add ground truth annotations to the commonsense_aligned_triplets
        for k, v, in self.triplets_train_gt.items():
            if k not in self.commonsense_aligned_triplets.keys():
                self.commonsense_aligned_triplets[k] = v
            else:
                self.commonsense_aligned_triplets[k] += v
        # remove ground truth annotations from the commonsense_violated_triplets
        self.commonsense_violated_triplets = {k: v for k, v in self.commonsense_violated_triplets.items() if k not in self.triplets_train_gt.keys()}

        print('len(self.triplets_train_gt), len(self.commonsense_violated_triplets), len(self.commonsense_aligned_triplets)',
              len(self.triplets_train_gt), len(self.commonsense_violated_triplets), len(self.commonsense_aligned_triplets))
        print('Saving triplets/commonsense_violated_triplets.pt and triplets/commonsense_aligned_triplets.pt')
        # torch.save(self.commonsense_violated_triplets, 'triplets/commonsense_violated_triplets.pt')
        # torch.save(self.commonsense_aligned_triplets, 'triplets/commonsense_aligned_triplets.pt')


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

