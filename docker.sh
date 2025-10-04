docker run --init -d \
  --name homeassistant \
  --restart=unless-stopped \
  -v /etc/localtime:/etc/localtime:ro \
  -v /home/homeAssistant/config:/config \
  --network=host \
  homeassistant/raspberrypi4-homeassistant:stable
