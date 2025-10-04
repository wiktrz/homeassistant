$ docker run \
   --name zigbee2mqtt \
   --restart=unless-stopped \
   --device=/dev/serial/by-id/usb-Itead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_V2_3a8edbda0774ef11ac78e41e313510fd-if00-port0:/dev/ttyUSB0 \
   -p 8080:8080 \
   -v $(pwd)/data:/app/data \
   -v /run/udev:/run/udev:ro \
   -e TZ=Europe/Amsterdam \
   ghcr.io/koenkk/zigbee2mqtt
