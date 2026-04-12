# UniFi AI Port

A DIY UniFi AI Port that runs on your TrueNAS box, spoofs as one or more
UniFi cameras in Protect, and injects real-time **person/vehicle smart
detections** from your existing RTSP cameras using YOLOv8 inference.

## What it does

- Each camera in your config appears as an adopted camera in UniFi Protect
- Person and vehicle detections show on Protect's timeline with bounding
  boxes and thumbnails
- Virtual line crossing events (configure via the built-in web tool)
- Fully automated adoption — no manual clicking in the Protect UI

## Hardware acceleration

The default image auto-detects and uses the fastest available runtime:

| Hardware | How to enable |
|---|---|
| **Intel iGPU** (N100 etc.) | Enable "Intel GPU" in the app settings |
| **CPU** | Works out of the box, no config needed |

For NVIDIA CUDA, use the standalone Docker image instead
(`Dockerfile.cuda`).

## After install

1. Open `http://<truenas-ip>:8091/` to access the line-drawing tool
2. Virtual lines are configured via the web tool and saved to config.yml
3. For advanced per-camera settings, edit `/config/config.yml` directly
   on the dataset you chose during install
