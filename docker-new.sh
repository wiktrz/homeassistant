docker run --init -d \
  --name homeassistant-new \
  --privileged \
  --restart=unless-stopped \
  -v /etc/localtime:/etc/localtime:ro \
  -v /home/homeAssistant/config:/config \
  --network=host \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  homeassistant/raspberrypi4-homeassistant:stable