
""" 
 Testing 
"""

import os
import time, json
import datetime
import numpy as np
import torch
import pdb
import pickle
import torch.utils.data as data_utils
from modules.evaluation import evaluate_frames
from modules.box_utils import decode, nms
from data import custum_collate
from modules import utils
import modules.evaluation as evaluate

logger = utils.get_logger(__name__)

def gen_dets(args, net, val_dataset):
    
    net.eval()
    val_data_loader = data_utils.DataLoader(val_dataset, int(args.BATCH_SIZE), num_workers=args.NUM_WORKERS,
                                 shuffle=False, pin_memory=True, collate_fn=custum_collate)
    for iteration in args.EVAL_ITERS:
        args.det_itr = iteration
        logger.info('Testing at ' + str(iteration))
        args.det_save_dir = "{pt:s}/detections-{it:06d}/".format(pt=args.SAVE_ROOT, it=iteration, )
        logger.info('detection saving dir is :: '+args.det_save_dir)

        if not os.path.isdir(args.det_save_dir): #if save directory doesn't exist create it
            os.makedirs(args.det_save_dir)
        
        args.MODEL_PATH = args.SAVE_ROOT + 'model_{:06d}.pth'.format(iteration)
        net.load_state_dict(torch.load(args.MODEL_PATH))
        
        logger.info('Finished loading model %d !' % iteration )
        
        torch.cuda.synchronize()
        tt0 = time.perf_counter()
        
        net.eval() # switch net to evaluation mode        
        mAP, _, ap_strs = perform_detection(args, net, val_data_loader, val_dataset, iteration)
        label_types = [args.label_types[0]] + ['ego_action']
        for nlt in range(len(label_types)):
            for ap_str in ap_strs[nlt]:
                logger.info(ap_str)
        ptr_str = '\n{:s} MEANAP:::=> {:0.5f}'.format(label_types[nlt], mAP[nlt])
        logger.info(ptr_str)

        torch.cuda.synchronize()
        logger.info('Complete set time {:0.2f}'.format(time.perf_counter() - tt0))


def perform_detection(args, net,  val_data_loader, val_dataset, iteration):

    """Test a network on a video database."""

    num_images = len(val_dataset)    
    print_time = True
    val_step = 50
    count = 0
    torch.cuda.synchronize()
    ts = time.perf_counter()
    activation = torch.nn.Sigmoid().cuda()

    ego_pds = []
    ego_gts = []

    det_boxes = []
    gt_boxes_all = []

    for nlt in range(1):
        numc = args.num_classes_list[nlt]
        det_boxes.append([[] for _ in range(numc)])
        gt_boxes_all.append([])
    
    nlt = 0

    with torch.no_grad():
        for val_itr, (images, gt_boxes, gt_targets, ego_labels, batch_counts, img_indexs, wh) in enumerate(val_data_loader):

            torch.cuda.synchronize()
            t1 = time.perf_counter()

            batch_size = images.size(0)
            
            images = images.cuda(0, non_blocking=True)
            decoded_boxes, confidence, ego_preds = net(images)
            ego_preds = activation(ego_preds).cpu().numpy()
            ego_labels = ego_labels.numpy()
            confidence = activation(confidence)
            seq_len = ego_preds.shape[1]
            
            if print_time and val_itr%val_step == 0:
                torch.cuda.synchronize()
                tf = time.perf_counter()
                logger.info('Forward Time {:0.3f}'.format(tf-t1))
            
            for b in range(batch_size):
                index = img_indexs[b]
                annot_info = val_dataset.ids[index]
                video_id, frame_num, step_size = annot_info
                videoname = val_dataset.video_list[video_id]
                for s in range(seq_len):
                    
                    if ego_labels[b,s]>-1:
                        ego_pds.append(ego_preds[b,s,:])
                        ego_gts.append(ego_labels[b,s])
                    
                    gt_boxes_batch = gt_boxes[b, s, :batch_counts[b, s],:].numpy()
                    gt_labels_batch =  gt_targets[b, s, :batch_counts[b, s]].numpy()
                    decoded_boxes_batch = decoded_boxes[b,s]
                    frame_gt = utils.get_individual_labels(gt_boxes_batch, gt_labels_batch[:,:1])
                    gt_boxes_all[0].append(frame_gt)
                    confidence_batch = confidence[b,s]
                    scores = confidence_batch[:, 0].squeeze().clone()
                    save_data = utils.filter_detections_with_confidences(args, scores, decoded_boxes_batch, confidence_batch)
                    # if save_data
                    # print(save_data.shape)
                    det_boxes[0][0].append(save_data[:, :5])
                    count += 1
                    save_dir = '{:s}/{}'.format(args.det_save_dir, videoname)
                    if not os.path.isdir(save_dir):
                        os.makedirs(save_dir)
                    save_name = '{:s}/{:08d}.pkl'.format(save_dir, frame_num+1)
                    frame_num += step_size
                    save_data = {'ego':ego_preds[b,s,:], 'main':save_data}
                    if s>=args.skip_beggning and s<seq_len-args.skip_ending:
                        with open(save_name,'wb') as ff:
                            pickle.dump(save_data, ff)

            if print_time and val_itr%val_step == 0:
                torch.cuda.synchronize()
                te = time.perf_counter()
                logger.info('im_detect: {:d}/{:d} time taken {:0.3f}'.format(count, num_images, te-ts))
                torch.cuda.synchronize()
                ts = time.perf_counter()
            if print_time and val_itr%val_step == 0:
                torch.cuda.synchronize()
                te = time.perf_counter()
                logger.info('NMS stuff Time {:0.3f}'.format(te - tf))

    mAP, ap_all, ap_strs = evaluate.evaluate(gt_boxes_all, det_boxes, args.all_classes, iou_thresh=args.IOU_THRESH)
    mAP_ego, ap_all_ego, ap_strs_ego = evaluate.evaluate_ego(np.asarray(ego_gts), np.asarray(ego_pds),  args.ego_classes)
    return mAP + mAP_ego, ap_all + ap_all_ego, ap_strs + ap_strs_ego


def gather_framelevel_detection(args, video_list):
    
    detections = {}
    for l, ltype in enumerate(args.label_types + ['av_action']):
        detections[ltype] = {}

    for videoname in video_list:       
        vid_dir = os.path.join(args.det_save_dir, videoname)
        frames_list = os.listdir(vid_dir)
        for frame_name in frames_list:
            if not frame_name.endswith('.pkl'):
                continue
            save_name = os.path.join(vid_dir, frame_name)
            with open(save_name,'rb') as ff:
                dets = pickle.load(ff)
            frame_name = frame_name.rstrip('.pkl')
            # detections[videoname+frame_name] = {}
            detections['av_action'][videoname+frame_name] = dets['ego']
            frame_dets = dets['main']
            start_id = 4
            for l, ltype in enumerate(args.label_types):
                numc = args.num_classes_list[l]
                ldets = get_ltype_dets(frame_dets, start_id, numc, ltype, args)
                detections[ltype][videoname+frame_name] = ldets
                start_id += numc

        logger.info('Done for ' + videoname)
        # break
    logger.info('Dumping detection in ' + args.det_file_name)
    with open(args.det_file_name, 'wb') as f:
            pickle.dump(detections, f)
    logger.info('Done dumping')


def get_ltype_dets(frame_dets, start_id, numc, ltype, args):
    dets = []
    for cid in range(numc):
        if frame_dets.shape[0]>0:
            boxes = frame_dets[:, :4].copy()
            scores = frame_dets[:, start_id+cid].copy()
            # cinds = scores>args.CONF_THRESH
            # boxes, scores = boxes[cinds,:], scores[cinds]
            pickn = min(100000, boxes.shape[0])
            cls_dets = np.hstack((boxes[:pickn,:], scores[:pickn, np.newaxis]))
        else:
            cls_dets = np.asarray([])
        dets.append(cls_dets)
    return dets


def eval_framewise_dets(args, val_dataset):
    for iteration in args.EVAL_ITERS:
        
        log_file = open("{pt:s}/frame-level-resutls-{it:06d}.log".format(pt=args.SAVE_ROOT, it=iteration), "w", 10)
        args.det_save_dir = "{pt:s}detections-{it:06d}/".format(pt=args.SAVE_ROOT, it=iteration)
        args.det_file_name = "{pt:s}frame-level-dets-{it:06d}.pkl".format(pt=args.SAVE_ROOT, it=iteration)
        # if not os.path.isfile(args.det_file_name):
        logger.info('Gathering detection at ' + str(iteration))
        # gather_framelevel_detection(args, val_dataset.video_list)
        logger.info('Done Gathering detections')
        result_file = args.SAVE_ROOT + '/frame-map-results.json'
        results = {}
        
        for subset in args.TEST_SUBSETS + args.VAL_SUBSETS:
            sresults = evaluate_frames(val_dataset.anno_file, args.det_file_name, subset, iou_thresh=0.5)
            for _, label_type in enumerate(args.label_types):
                name = subset + ' & ' + label_type
                rstr = '\n\nResults for ' + name + '\n'
                logger.info(rstr)
                log_file.write(rstr+'\n')
                results[name] = {'mAP': sresults[label_type]['mAP'], 'APs': sresults[label_type]['ap_all']}
                for ap_str in sresults[label_type]['ap_strs']:
                    logger.info(ap_str)
                    log_file.write(ap_str+'\n')
                    
            with open(result_file, 'w') as f:
                json.dump(results, f)