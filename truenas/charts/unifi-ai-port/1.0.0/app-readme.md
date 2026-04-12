# UniFi AI Port

A DIY UniFi AI Port that runs on your TrueNAS box, spoofs as one or more
UniFi cameras in Protect, and injects real-time **person/vehicle smart
detections** from your existing RTSP cameras using YOLOv8 inference.

## Setup

1. Install the app — the wizard only asks for your Protect host,
   credentials, storage path, and GPU toggle
2. Open `http://<truenas-ip>:8091/` (or your chosen port)
3. Use the **Setup** tab to add cameras and configure AI settings
4. Use the **Lines** tab to draw virtual crossing lines on live frames
5. Click **Save** and restart the container to apply

All configuration is managed through the web UI. Advanced users can
also edit `config.yml` directly on the dataset.

## Hardware acceleration

The default image auto-detects the fastest available runtime. Enable
"Intel GPU Passthrough" in the app settings to use your Intel iGPU.
