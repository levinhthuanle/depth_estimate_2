import torch
import cv2

import onnxruntime as ort

from torchvision.transforms import Compose
from transforms import Resize, NormalizeImage, PrepareForNet
from dataloader import LoadStreams
from tqdm import tqdm
import numpy as np
from function import specialCase as specialCase

global dem
dem = 0
baseline = 0.016 # meter
weights = "weights/yolov7n.onnx"

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

available_provider_names = ort.get_available_providers()
# load model from onnx
model = ort.InferenceSession(weights, providers=available_provider_names)

net_w, net_h = 256, 256
resize_mode = "upper_bound"
normalization = NormalizeImage(
    mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
)

transform = Compose(
    [
        Resize(
            net_w,
            net_h,
            resize_target=None,
            keep_aspect_ratio=False,
            ensure_multiple_of=32,
            resize_method=resize_mode,
            image_interpolation_method=cv2.INTER_CUBIC,
        ),
        normalization,
        PrepareForNet(),
    ]
)

dataset = LoadStreams(sources="0", img_size=net_w, transforms=transform)
bs = len(dataset)

def predict(image, im0s=None):
    im = torch.from_numpy(image).to(device)
    im /= 255

    if device == torch.device("cuda"):
        im = im.to(memory_format=torch.channels_last)

    if len(im.shape) == 3:
        im = im[None]  # expand for batch dim

    if im0s:
        img_shape = im0s[0].shape[:2]
    else:
        img_shape = im.shape[2:]

    # inference
    input_name = model.get_inputs()[0].name
    output_name = model.get_outputs()[0].name
    prediction = model.run([output_name], {input_name: im.cpu().numpy()})[0]
    prediction = torch.from_numpy(prediction).to(device)

    prediction = (
        torch.nn.functional.interpolate(
            prediction.unsqueeze(1),
            size=img_shape,
            mode="bicubic",
            align_corners=False,
        )
        .squeeze()
        .cpu()
        .numpy()
    )

    depth_min = prediction.min()
    depth_max = prediction.max()
    bits = 2

    max_val = (2**(8*bits))-1

    if depth_max - depth_min > np.finfo("float").eps:
        out = max_val * (prediction - depth_min) / (depth_max - depth_min)
    else:
        out = np.zeros(prediction.shape, dtype=prediction.type)

    out = out.astype(np.uint16)
    return out


def convert_depth_to_distance(depth, camera_matrix, baseline):
    fx = camera_matrix[0, 0]
    fy = camera_matrix[1, 1]
    cx = camera_matrix[0, 2]
    cy = camera_matrix[1, 2]

    depth = depth.astype(np.float32)
    depth[depth == 0] = 0.0001
    distance = baseline * fx * depth
    # distance[distance > 10000] = 10000
    return distance

def load_camera_matrix(file):
    with open(file, "r") as f:
        lines = f.readlines()
        lines = [line.strip() for line in lines]
        mtx = lines[1:4]
        dist = lines[5:6]
        mtx = [line.split(" ") for line in mtx]
        mtx = [[float(x) for x in line] for line in mtx]
        mtx = np.array(mtx)
        dist = [line.split(" ") for line in dist]
        dist = [[float(x) for x in line] for line in dist]
        dist = np.array(dist)
    return mtx, dist

camera_matrix, dist = load_camera_matrix("calibration.txt")


for path, img, im0s, vid_cap, s in dataset:
    depth = predict(img, im0s)
    depth = depth / depth.max()
    depth = depth.max() - depth
    distance = convert_depth_to_distance(depth, camera_matrix, baseline)

    width, height = distance.shape
    w_3 = int(width / 3)
    h_3 = int(height / 3)
    
    # draw 9 boxes 
    for i in range(3):
        for j in range(3):
            cv2.rectangle(im0s[0], (j*h_3, i*w_3), ((j+1)*h_3, (i+1)*w_3), (0, 255, 0), 1)

    # get 9 points in 9 boxes
    points = []
    for i in range(3):
        for j in range(3):
            x = int(j*h_3 + h_3/2)
            y = int(i*w_3 + w_3/2)
            points.append([x, y])
    points = np.array(points)
    
    # get distance of 5 points
    distances = distance[points[:, 1], points[:, 0]]
    
    # draw 5 points
    for point in points:
        cv2.circle(im0s[0], tuple(point), 5, (0, 0, 255), -1)

    # draw distance text
    for i, point in enumerate(points):
        cv2.putText(im0s[0], f"{distances[i]:.2f} m", tuple(point), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)


    # map
    map = np.zeros_like(distance)
    for i in range(3):
        for j in range(3):
            dis = distance[i*w_3:(i+1)*w_3, j*h_3:(j+1)*h_3]
            # mean distance
            dis = dis.mean()
            map[i*w_3:(i+1)*w_3, j*h_3:(j+1)*h_3] = dis

    map_color = cv2.normalize(map, None, 0, 255, cv2.NORM_MINMAX)
    map_color = cv2.applyColorMap(map_color.astype(np.uint8), cv2.COLORMAP_JET)
    a = []
    # draw dis text in center of map
    for i in range(3):
        a.append([])
        for j in range(3):
            dis = map[i*w_3:(i+1)*w_3, j*h_3:(j+1)*h_3].mean()
            a[i].append(dis)
            x = int(j*h_3 + h_3/2)
            y = int(i*w_3 + w_3/2)
            cv2.putText(map_color, f"{dis:.2f} m", (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

    distance = cv2.normalize(distance, None, 0, 255, cv2.NORM_MINMAX)
    distance = cv2.applyColorMap(distance.astype(np.uint8), cv2.COLORMAP_JET)
    dem += 1
    ans = ''
    if dem % 5 == 0:
        ans = specialCase(a)
        print(ans)
    cv2.imshow("depth", depth)
    cv2.imshow("map", map_color)
    cv2.imshow("distance", distance)
    cv2.imshow("image", im0s[0])
    if cv2.waitKey(1) == ord("q"):
        break
