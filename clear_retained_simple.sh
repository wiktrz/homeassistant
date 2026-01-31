#!/bin/bash

# MQTT Retained Messages Cleaner
# This script discovers and clears all retained messages from the MQTT broker

# MQTT Configuration
MQTT_HOST="10.20.2.111"
MQTT_USER="mqtt"
MQTT_PASS="waders"
MQTT_PORT="1883"

echo "🧹 MQTT Retained Messages Cleaner"
echo "=================================="
echo "Host: $MQTT_HOST:$MQTT_PORT"
echo "User: $MQTT_USER"
echo ""

# Function to clear retained messages for a specific topic
clear_topic() {
    local topic="$1"
    echo "  🗑️  Clearing: $topic"
    mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -p "$MQTT_PORT" -t "$topic" -n
}

# Get all topics with retained messages
echo "🔍 Discovering topics with retained messages..."

# Create a temporary file to store topics
TEMP_FILE=$(mktemp)

# Subscribe to all topics and capture retained messages
mosquitto_sub -v -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -p "$MQTT_PORT" -t '#' > "$TEMP_FILE" &
SUB_PID=$!

# Wait for retained messages to be received
sleep 5

# Kill the subscription process
kill $SUB_PID 2>/dev/null
wait $SUB_PID 2>/dev/null

# Check if we got any data
if [ ! -s "$TEMP_FILE" ]; then
    echo "❌ No MQTT data received. Check connection and credentials."
    rm -f "$TEMP_FILE"
    exit 1
fi

# Extract unique topics from the captured output
echo "📋 Found topics with retained messages:"
echo ""

# Count topics for summary
TOPIC_COUNT=0

grep -o '^[^[:space:]]*' "$TEMP_FILE" | grep -v '^$' | sort -u | while read -r topic; do
    if [ -n "$topic" ] && [ "$topic" != "Client" ] && ! echo "$topic" | grep -q '^[0-9]*$'; then
        clear_topic "$topic"
        sleep 0.05  # Small delay to avoid overwhelming the broker
        TOPIC_COUNT=$((TOPIC_COUNT + 1))
    fi
done

# Clean up
rm -f "$TEMP_FILE"

echo ""
echo "✅ Retained message clearing completed!"
echo "📊 Processed topics: $TOPIC_COUNT"
echo ""
echo "💡 To monitor MQTT topics:"
echo "   mosquitto_sub -v -h $MQTT_HOST -u $MQTT_USER -P $MQTT_PASS -p $MQTT_PORT -t '#'"

