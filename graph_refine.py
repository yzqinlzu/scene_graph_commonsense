import torch
import os
from PIL import Image
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
import transformers
from transformers import CLIPProcessor, CLIPModel, AutoTokenizer
from collections import deque

from evaluate import inference, eval_pc
from utils import *
from dataset_utils import relation_by_super_class_int2str


def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12356'
    dist.init_process_group("gloo", rank=rank, world_size=world_size)


class ImageGraph:
    def __init__(self):
        # node to neighbors mapping
        self.adj_list = {}
        # edge to nodes mapping
        self.edge_node_map = {}
        # store all nodes and their degree
        self.nodes = []
        self.edges = []

    def add_edge(self, subject_bbox, object_bbox, relation_id, string):
        subject_bbox, object_bbox = tuple(subject_bbox), tuple(object_bbox)
        edge = (subject_bbox, relation_id, object_bbox, string)
        edge_wo_string = (subject_bbox, relation_id, object_bbox)

        if edge not in self.edges:
            self.edges.append(edge_wo_string)
        if subject_bbox not in self.nodes:
            self.nodes.append(subject_bbox)
        if object_bbox not in self.nodes:
            self.nodes.append(object_bbox)

        print('subject_bbox', subject_bbox)
        print('object_bbox', object_bbox)
        print('edge', edge, '\n')

        # Check if the node is already present, otherwise initialize with an empty list
        if subject_bbox not in self.adj_list:
            self.adj_list[subject_bbox] = []
        if object_bbox not in self.adj_list:
            self.adj_list[object_bbox] = []

        # change list as immutable tuple as the dict key
        self.adj_list[subject_bbox].append(edge)
        self.adj_list[object_bbox].append(edge)

        self.edge_node_map[edge_wo_string] = (subject_bbox, object_bbox)

    def get_edge_neighbors(self, edge, hops=1):
        # find all the edges belonging to the 1-hop neighbor of the current edge
        curr_pair = self.edge_node_map[edge]
        subject_node, object_node = curr_pair[0], curr_pair[1]

        # find all edges connecting to the current subject and object node
        neighbor_edges = self.adj_list[subject_node] + self.adj_list[object_node]

        # remove the current edge from the set
        for neighbor_edge in neighbor_edges:
            if neighbor_edge[:-1] == edge:
                neighbor_edges.remove(neighbor_edge)

        if hops == 1:
            return set(neighbor_edges)

        elif hops == 2:
            # copy all hop1 edges
            hop2_neighbor_edges = [hop1_edge for hop1_edge in neighbor_edges]

            for hop2_edge in neighbor_edges:
                curr_pair = self.edge_node_map[hop2_edge[:-1]]
                subject_node, object_node = curr_pair[0], curr_pair[1]
                hop2_neighbor_edges += self.adj_list[subject_node] + self.adj_list[object_node]
                # don't have to remove curr hop2_edge because it is already in neighbor_edges and a set operation is enough

            # remove the current edge from the set by any chance
            for hop2_neighbor_edge in hop2_neighbor_edges:
                if hop2_neighbor_edge[:-1] == edge:
                    hop2_neighbor_edges.remove(hop2_neighbor_edge)

            return set(hop2_neighbor_edges)

        else:
            assert hops == 1 or hops == 2, "Not implemented"

    def get_node_degrees(self):
        degrees = {node: len(self.adj_list[node]) for node in self.adj_list}
        return degrees


def colored_text(text, color_code):
    return f"\033[{color_code}m{text}\033[0m"


def save_png(image, save_name="image.png"):
    image = image.mul(255).cpu().byte().numpy()  # convert to 8-bit integer values
    image = Image.fromarray(image.transpose(1, 2, 0))  # transpose dimensions for RGB order
    image.save(save_name)


def extract_words_from_edge(phrase, all_relation_labels):
    # iterate through the phrase to extract the parts
    for i in range(len(phrase)):
        if phrase[i] in all_relation_labels:
            relation = phrase[i]
            subject = " ".join(phrase[:i])
            object = " ".join(phrase[i + 1:])
            break  # exit loop once the relation is found

    return subject, relation, object


def crop_image(image, edge, args, crop=True):
    # crop out the subject and object from the image
    width, height = image.shape[1], image.shape[2]
    subject_bbox = torch.tensor(edge[0]) / args['models']['feature_size']
    object_bbox = torch.tensor(edge[2]) / args['models']['feature_size']
    subject_bbox[:2] *= height
    subject_bbox[2:] *= width
    object_bbox[:2] *= height
    object_bbox[2:] *= width
    # print('image', image.shape, 'subject_bbox', subject_bbox, 'object_bbox', object_bbox)

    # create the union bounding box
    union_bbox = torch.zeros(image.shape[1:], dtype=torch.bool)
    union_bbox[int(subject_bbox[2]):int(subject_bbox[3]), int(subject_bbox[0]):int(subject_bbox[1])] = 1
    union_bbox[int(object_bbox[2]):int(object_bbox[3]), int(object_bbox[0]):int(object_bbox[1])] = 1

    if crop:
        # find the minimum rectangular bounding box around the union bounding box
        nonzero_indices = torch.nonzero(union_bbox)
        min_row = nonzero_indices[:, 0].min()
        max_row = nonzero_indices[:, 0].max()
        min_col = nonzero_indices[:, 1].min()
        max_col = nonzero_indices[:, 1].max()

        # crop the image using the minimum rectangular bounding box
        cropped_image = image[:, min_row:max_row + 1, min_col:max_col + 1]

        # print('Cropped Image:', cropped_image.shape)
        return cropped_image
    else:
        return image * union_bbox


def clip_zero_shot(model, processor, image, edge, rank, args, based_on_hierar=True):
    # prepare text labels from the relation dictionary
    labels = list(relation_by_super_class_int2str().values())
    labels_geometric = labels[:args['models']['num_geometric']]
    labels_possessive = labels[args['models']['num_geometric']:args['models']['num_geometric']+args['models']['num_possessive']]
    labels_semantic = labels[-args['models']['num_semantic']:]

    # extract current subject and object from the edge
    phrase = edge[-1].split()
    subject, relation, object = extract_words_from_edge(phrase, labels)

    if based_on_hierar:
        # assume the relation super-category has a high accuracy
        if relation in labels_geometric:
            queries = [f"a photo of a {subject} {label} {object}" for label in labels_geometric]
        elif relation in labels_possessive:
            queries = [f"a photo of a {subject} {label} {object}" for label in labels_possessive]
        else:
            queries = [f"a photo of a {subject} {label} {object}" for label in labels_semantic]
    else:
        queries = [f"a photo of a {subject} {label} {object}" for label in labels]

    # crop out the subject and object from the image
    cropped_image = crop_image(image, edge, args)
    save_png(cropped_image, "cropped_image.png")

    # inference CLIP
    inputs = processor(text=queries, images=image, return_tensors="pt", padding=True).to(rank)
    outputs = model(**inputs)
    logits_per_image = outputs.logits_per_image  # image-text similarity score
    probs = logits_per_image.softmax(dim=1)  # label probabilities

    # get top predicted label
    top_label_idx = probs.argmax().item()
    top_label_str = relation_by_super_class_int2str()[top_label_idx]

    # show the results
    light_blue_code = 94
    light_pink_code = 95
    text_blue_colored = colored_text(top_label_str, light_blue_code)
    text_pink_colored = colored_text(relation, light_pink_code)
    print(f"Top predicted label from zero-shot CLIP: {text_blue_colored} (probability: {probs[0, top_label_idx]:.4f}), Target label: {text_pink_colored}\n")


def train_graph(model, tokenizer, processor, image, subject_node, object_node, subject_neighbor_edges, object_neighbor_edges, rank, args):
    neighbor_phrases = []
    neighbor_text_embeds = []

    all_neighbor_edges = subject_neighbor_edges + object_neighbor_edges

    # collect all neighbors of the current edge
    for neighbor_edge in all_neighbor_edges:
        phrase = neighbor_edge[-1]  # assume neighbor edges are the ground truths
        neighbor_phrases.append(phrase)

        inputs = tokenizer([f"a photo of a {phrase}"], padding=False, return_tensors="pt").to(rank)
        text_embed = model.get_text_features(**inputs)
        neighbor_text_embeds.append(text_embed)

    neighbor_text_embeds = torch.stack(neighbor_text_embeds)
    print('neighbor_phrases', neighbor_phrases)
    print('neighbor_text_embeds', neighbor_text_embeds.shape)

    # collect image embedding
    inputs = processor(images=image, return_tensors="pt").to(rank)
    image_embed = model.get_image_features(**inputs)
    print('image_embed', image_embed.shape)


def bfs_explore(image, graph, rank, args):
    # initialize CLIP
    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(rank)
    processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    tokenizer = AutoTokenizer.from_pretrained("openai/clip-vit-base-patch32")

    # get the node with the highest degree
    node_degrees = graph.get_node_degrees()
    print('node_degrees', node_degrees)
    start_node = max(node_degrees, key=node_degrees.get)

    # initialize queue and visited set for BFS
    queue = deque([(start_node, 0)])  # the second element in the tuple is used to keep track of levels
    visited = set()

    while True:
        while queue:
            # dequeue the next node to visit
            current_node, level = queue.popleft()

            # if the node hasn't been visited yet
            if current_node not in visited:
                print(f"Visiting node: {current_node} at level {level}")

                # mark the node as visited
                visited.add(current_node)

                # get all the neighboring edges for the current node
                neighbor_edges = graph.adj_list[current_node]

                # create a mapping from neighbor_node to neighbor_edge
                neighbor_to_edge_map = {edge[2] if edge[2] != current_node else edge[0]: edge for edge in neighbor_edges}

                # extract neighbor nodes and sort them by their degree
                neighbor_nodes = [edge[2] if edge[2] != current_node else edge[0] for edge in neighbor_edges]  # the neighbor node could be either the subject or the object
                neighbor_nodes = sorted(neighbor_nodes, key=lambda x: node_degrees.get(x, 0), reverse=True)

                # add neighbors to the queue for future exploration
                for neighbor_node in neighbor_nodes:
                    if neighbor_node not in visited:
                        neighbor_edge = neighbor_to_edge_map[neighbor_node]
                        print(f"Edge for next neighbor: {neighbor_edge}")

                        if args['training']['run_mode'] == 'clip_zs':
                            # query CLIP on the current neighbor edge in zero shot
                            clip_zero_shot(model, processor, image, neighbor_edge, rank, args)
                        else:
                            # train the model to predict relations from neighbors and image features
                            object_neighbor_edges = graph.adj_list[neighbor_node]
                            train_graph(model, tokenizer, processor, image, current_node, neighbor_node, neighbor_edges, object_neighbor_edges, rank, args)

                        queue.append((neighbor_node, level + 1))

        print("Finished BFS for current connected component.\n")

        # check if there are any unvisited nodes
        unvisited_nodes = set(node_degrees.keys()) - visited
        if not unvisited_nodes:
            break  # all nodes have been visited, exit the loop

        # start a new BFS from the unvisited node with the highest degree
        new_start_node = max(unvisited_nodes, key=lambda x: node_degrees.get(x, 0))
        print(f"Starting new BFS from node: {new_start_node}")
        queue.append((new_start_node, 0))


def process_sgg_results(rank, args, sgg_results):
    top_k_predictions = sgg_results['top_k_predictions']
    print('top_k_predictions', top_k_predictions[0])
    top_k_image_graphs = sgg_results['top_k_image_graphs']
    images = sgg_results['images']

    image_graphs = []
    for batch_idx, (curr_strings, curr_image) in enumerate(zip(top_k_predictions, top_k_image_graphs)):
        graph = ImageGraph()

        for string, triplet in zip(curr_strings, curr_image):
            subject_bbox, relation_id, object_bbox = triplet[0], triplet[1], triplet[2]
            graph.add_edge(subject_bbox, object_bbox, relation_id, string)
        print('graph.adj_list', graph.adj_list)

        bfs_explore(images[batch_idx], graph, rank, args)

        image_graphs.append(graph)

        break

    return image_graphs


def query_clip(gpu, args, test_dataset):
    rank = gpu
    world_size = torch.cuda.device_count()
    setup(rank, world_size)
    print('rank', rank, 'torch.distributed.is_initialized', torch.distributed.is_initialized())

    test_sampler = torch.utils.data.distributed.DistributedSampler(test_dataset, num_replicas=world_size, rank=rank)
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=args['training']['batch_size'], shuffle=False, collate_fn=collate_fn, num_workers=0, drop_last=True, sampler=test_sampler)
    print("Finished loading the datasets...")

    # receive current SGG predictions from a baseline model
    sgg_results = eval_pc(rank, args, test_loader, return_sgg_results=True, top_k=5)

    # iterate through the generator to receive results
    for batch_idx, batch_sgg_results in enumerate(sgg_results):
        print('batch_idx', batch_idx)
        image_graphs = process_sgg_results(rank, args, batch_sgg_results)

    dist.destroy_process_group()  # clean up

