# Morning Music Directory (Poranna Muzyka)

This directory contains audio files (`.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`) used by the two-step morning wake-up automation (`morning_radio_schedule`) and `script.play_morning_music`.

- **How it works:** Every morning at wake-up time, Home Assistant randomly selects one audio track from this directory and plays it on Home Assistant Voice PE for ~3 minutes before switching to the scheduled live radio station (Radio ZET on weekdays, Antyradio on weekends).
- **Managing tracks:** You can add, replace, or remove music files directly here in macOS Finder or via Home Assistant Media Browser.
- **Fail-safe:** If this folder is empty, the system automatically skips directly to the radio station so you never miss an alarm.
