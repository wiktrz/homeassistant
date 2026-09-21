# Plan Udoskonalenia Home Assistant pod Asystenta Głosowego AI (Voice AI)

> **Status dokumentu:** Do przeglądu / Propozycja architektoniczna (niezaimplementowana)  
> **Projekt:** `/Users/wtrzonkowski/Desktop/private/homeassistant`  
> **Kontekst ekosystemu:** Integracja z ESP32 (`home_automation`), BoneIO (DIN), Shelly RGBW, Pstryk (cena dynamiczna), Reolink, Tesla, Odpady  
> **Cel główny:** Przygotowanie encji, automatyzacji i struktury Home Assistant do natywnej, bezbłędnej i intuicyjnej obsługi głosowej w języku polskim (HA Assist / Google Gemini / OpenAI / Wyoming Satellites).

---

## Spis Treści
1. [Podsumowanie Wykonawcze i Stan Obecny](#1-podsumowanie-wykonawcze-i-stan-obecny)
2. [Audyt Architektury Encji – Problemy i Ryzyka](#2-audyt-architektury-encji--problemy-i-ryzyka)
3. [Audyt Automatyzacji – Konflikty, Opaque GUIDs i Pętle](#3-audyt-automatyzacji--konflikty-opaque-guids-i-p%C4%99tle)
4. [Luki w Gotowości pod Voice AI (Assist / LLM)](#4-luki-w-gotowo%C5%9Bci-pod-voice-ai-assist--llm)
5. [Pakiet Usprawnień 1: Remediacja i Standaryzacja Encji](#5-pakiet-usprawnie%C5%84-1-remediacja-i-standaryzacja-encji)
6. [Pakiet Usprawnień 2: Konsolidacja i Naprawa Automatyzacji](#6-pakiet-usprawnie%C5%84-2-konsolidacja-i-naprawa-automatyzacji)
7. [Pakiet Usprawnień 3: Projekt Interakcji Głosowych i Nowe Makra/Sceny](#7-pakiet-usprawnie%C5%84-3-projekt-interakcji-g%C5%82osowych-i-nowe-makrasceny)
8. [Pakiet Usprawnień 4: Architektura Silnika Voice AI (Assist / Gemini / Whisper)](#8-pakiet-usprawnie%C5%84-4-architektura-silnika-voice-ai-assist--gemini--whisper)
9. [Matryca Wdrożenia i Kolejność Prac](#9-matryca-wdro%C5%BCenia-i-kolejno%C5%9B%C4%87-prac)

---

## 1. Podsumowanie Wykonawcze i Stan Obecny

System Home Assistant w tej instalacji zarządza zaawansowanym domem prywatnym (`local00`) oraz 6 lokalami na wynajem (`local01`–`local06`). Posiada solidną bazę sprzętową:
- Przewodowe sterowniki przekaźników i wejść cyfrowych **BoneIO** (DIN) dla oświetlenia i przycisków,
- Kontrolery oświetlenia dekoracyjnego **Shelly RGBW2** oraz **Shelly Plus RGBW PM**,
- Autorski system mikrokontrolerów **ESP32** z magistralami OneWire DS18B20, przekaźnikami CO i termostatami ściennymi TTGO T4,
- Integrację taryf dynamicznych energii **Pstryk** (dwie strefy: dół i góra),
- Kamery i syreny **Reolink**,
- Integrację kalendarza wywozu odpadów komunalnych oraz ładowania pojazdu Tesla.

Jednakże obecna konfiguracja powstała w modelu tradycyjnego sterowania z poziomu dashboardu i fizycznych włączników. **Dla asystenta głosowego (Voice AI) system wykazuje obecnie szereg krytycznych barier**:
1. **Brak definicji Pokojów/Stref (Brak Areas):** Rejestr `core.area_registry` jest pusty. Asystent nie wie, które światła ani termostaty znajdują się w jakim pomieszczeniu.
2. **Krytyczne błędy domenowe:** Rygiel drzwi wejściowych (`light.boneio_32_l_07_73bbd8_door_23_relay`) oraz podczerwień kamery są zdefiniowane jako… **światła** (`light`). Komenda głosowa *"Wyłącz wszystkie światła"* skutkuje wyzwoleniem rygla drzwi wejściowych!
3. **Brak domeny Rolet (`cover`):** Roleta w sypialni to 5 rozproszonych skryptów operujących na surowych przekaźnikach switch z twardymi opóźnieniami (`delay`). Asystent nie rozumie poleceń *"Otwórz roletę"*, *"Zasłoń roletę do połowy"*, *"Zatrzymaj roletę"*.
4. **Niekompletne termostaty `local00`:** W `mqtt.yaml` zdefiniowano encje `climate` tylko dla 8 stref – brakuje ich dla Kuchni, Małej Łazienki, Pomieszczenia Gospodarczego, Wiatrołapu i Holu. Żaden z termostatów nie posiada `mode_command_topic`.
5. **Konfliktujące automatyzacje:** Aż 7 niezależnych automatyzacji steruje równolegle światłem w Małej Łazience, co powoduje wyścigi stanów (race conditions).
6. **Opaque GUIDs:** Wiele automatyzacji używa wewnętrznych identyfikatorów szesnastkowych zamiast semantycznych encji, co uniemożliwia ich analizę przez modele językowe (LLM).

---

## 2. Audyt Architektury Encji – Problemy i Ryzyka

### 2.1. Poważne Nieprawidłowości Domenowe (Domain Misclassifications)

| Encja | Obecna domena | Prawidłowa domena | Wpływ na Asystenta Głosowego |
|---|---|---|---|
| `light.boneio_32_l_07_73bbd8_door_23_relay` | `light` | `lock` / `button` / `event` | **KRYTYCZNY:** Komenda *"Wyłącz wszystkie światła"* wyłącza/przełącza rygiel drzwi. Asystent traktuje drzwi jako żarówkę. |
| `light.520a_infra_red_lights_in_night_mode` | `light` | `switch` | Dioda IR kamery zewnętrznej reaguje na zbiorcze komendy oświetleniowe w strefie zewnętrznej. |
| `light.boneio_32_l_07_new_light_11` (Taras Zasilanie) | `light` | `switch` | Zasilacz LED tarasu jako osobne światło. Przy *"włącz światła na tarasie"* asystent włącza zasilacz, ale nie lampy (lub odwrotnie). |
| Roleta sypialni (`roller_bedroom_*`) | Brak (`script`) | `cover` (`template` / `cover_time_based`) | Brak obsługi standardowych intencji głosowych domeny `cover` (Otwórz / Zamknij / Stop / Ustaw poziom). |

### 2.2. Braki w Encjach Termostatów (`climate`) dla Domu (`local00`)
W `config/mqtt.yaml` encje klimatyzacji/ogrzewania zdefiniowano dla:
- `local00_klatka1p_termostat`, `local00_klatka2p_termostat`, `local00_salon_termostat`, `local00_sypialnia_termostat`, `local00_lazienka_termostat`, `local00_klara_termostat`, `local00_nikola_termostat`, `local00_gabinet_termostat`.

**Brakuje encji `climate` dla kluczowych pomieszczeń:**
- **Kuchnia** (istnieje tylko sensor temperatury i grzania: `sensor.local00_kuchnia_*`),
- **Mała Łazienka** (`sensor.local00_malalazienka_*`),
- **Wiatrołap** (`sensor.local00_wiatrolap_*`),
- **Hol Wejściowy** (`sensor.local00_holwejscie_*`),
- **Hol Sypialnie** (`sensor.local00_holsypialnia_*`),
- **Pomieszczenie Gospodarcze** (`sensor.local00_gospodarcze_*`).

> **Skutek dla głosu:** Użytkownik pytający *"Ustaw 21 stopni w kuchni"* lub *"Ustaw temperaturę w małej łazience na 23"* otrzymuje błąd *"Nie znaleziono takiego termostatu"*.

Dodatkowo żaden termostat nie definiuje `mode_command_topic`. Próba wyłączenia lub włączenia grzania głosem (*"Wyłącz ogrzewanie w salonie"*) kończy się błędem w HA.

### 2.3. Brakujące Pomocniki (Missing Helpers) Wykryte w Logach i Automatyzacjach
W kodzie automatyzacji odpytywane są encje, które w ogóle nie istnieją w `configuration.yaml` ani w rejestrze:
- `input_select.shelly_mode` *(powoduje błąd w logu: `unknown entity input_select.shelly_mode`)*,
- `input_number.shelly_brightness`,
- `input_datetime.pora_nocna`,
- `input_datetime.pora_pobudka`,
- `input_datetime.pora_pobudka_weekend`,
- `input_datetime.pora_switu`,
- `input_datetime.pora_zmroku` oraz `input_datetime.pora_zmroku_na_zewnatrz` *(używane zamiennie w różnych automatyzacjach!)*,
- `input_button.btn_entrance_door`,
- `input_boolean.kitchen_helper`,
- `input_boolean.hall_entrance_input_manual`.

---

## 3. Audyt Automatyzacji – Konflikty, Opaque GUIDs i Pętle

### 3.1. Przeludnienie i Wojna Automatyzacji w Małej Łazience
W `config/automations.yaml` zidentyfikowano aż **6 aktywnych automatyzacji** sterujących tym samym obwodem w małej łazience:
1. `1740228530753` – *PIR Bathroom Small ON*
2. `1740228697228` – *PIR Bathroom Small Off*
3. `1746970911150` – *Reed_Small_Bathroom_OFF*
4. `1747671242888` – *Reed Small Bathroom ON v2*
5. `1754334259452` – *Merged Small Bathroom Door Reed Automation*
6. `1754334356656` – *Small Bathroom Motion Automation*

Gdy asystent głosowy włącza światło w małej łazience, wygaszacz z automatyzacji PIR lub kontaktronu po 30 sekundach potrafi zgasić światło użytkownikowi, ponieważ brakuje flagi blokady/manual override.

### 3.2. Duplikaty Automatyzacji Ściemniania Wschód/Zachód
Dwie automatyzacje o identycznej nazwie wyzwalane są o poranku na bazie kąta słońca i publikują sprzeczne wartości jasności do tego samego Shelly RGBW:
- `d31bdcaa032d42628e404ca935baa872`: Trigger: `sun.sun elevation > -4.0` -> `gain: 0, white: 100`
- `9c3c8f9c510041ec87050e35fc301091`: Trigger: `sun.sun elevation > 0` -> `gain: 100, white: 255`

### 3.3. Skrypt `all_lights_off` Operujący na Nieistniejących Strukturach
Skrypt `script.all_lights_off` zawiera:
```yaml
- action: switch.turn_off
  target:
    floor_id: parter       # BŁĄD: Piętra (floors) nie są skonfigurowane w HA!
- action: light.turn_off
  target:
    label_id: swiatlo      # BŁĄD: Etykieta (label) 'swiatlo' nie istnieje w HA!
- action: input_select.select_option
  target:
    entity_id: input_select.shelly_mode # BŁĄD: Nieistniejąca encja!
```
W rezultacie polecenie *"Wyłącz wszystkie światła"* nie wyłącza poprawnie urządzeń lub zgłasza błędy w tle.

### 3.4. Opaque Device IDs / Hex Entity IDs
Wiele reguł zostało wyklikanych w GUI i zapisało się w formacie:
```yaml
actions:
  - device_id: e15a1fb7435cfe16a86b0f58812a81c7
    domain: light
    entity_id: 9afa6453bc16d6061e60df5649a8a099
    type: turn_on
```
Takie zapisy uniemożliwiają asystentom LLM (OpenAI, Gemini) zrozumienie logiki automatyzacji, uniemożliwiają dynamiczne przełączanie przez nazwy i są podatne na awarie przy reinstalacji urządzeń.

---

## 4. Luki w Gotowości pod Voice AI (Assist / LLM)

### 4.1. Brak Podziału na Obszary (Areas)
W asystencie Home Assistant Assist kluczową rolę pełni relacja **Obszar (Area) ↔ Urządzenie (Device) ↔ Satelita głosu**. 
- Gdy mikrofon (np. w salonie) słyszy *"Włącz światło"*, Assist sprawdza, w jakim Area znajduje się ten mikrofon i włącza światła tylko w tym pokoju.
- Bez skonfigurowanych Areas asystent musi za każdym razem pytać *"Które światło masz na myśli?"*.

### 4.2. Język Pipeline'u w `.storage/assist_pipeline.pipelines`
Obecnie w `.storage`:
```json
"conversation_language": "en",
"language": "en"
```
Pipeline jest ustawiony na język angielski, podczas gdy wszystkie nazwy encji, aliasy w `customize.yaml` oraz polecenia domowników formułowane są po polsku.

### 4.3. Zanieczyszczenie Kontekstu (Context Pollution) w Modelach LLM
W instalacji istnieje ponad 150 sensorów MQTT, w tym stany przekaźników dla 6 mieszkań na wynajem (`local01`–`local06`).
- Jeśli model językowy (np. Gemini / GPT-4o-mini) otrzyma w promptcie wszystkie encje, zużyje tysiące tokenów na każde zapytanie, czas odpowiedzi wzrośnie o 2–4 sekundy, a asystent zacznie mylić salon domowy z lokalem 02.
- Konieczne jest **odcięcie mieszkań na wynajem od domowego asystenta głosowego** poprzez flagę `expose_to_assist: false`.

---

## 5. Pakiet Usprawnień 1: Remediacja i Standaryzacja Encji

### 5.1. Naprawa Drzwi i Rygla (Domain Remapping)
Wycofać `door_23_relay` z domeny `light`. Skonfigurować poprawną encję rygla jako zamek (`lock`) lub przycisk (`button`):

```yaml
# Propozycja w configuration.yaml (Template Lock):
lock:
  - platform: template
    name: "Rygiel Drzwi Wejściowych"
    unique_id: rygiel_drzwi_wejsciowych
    value_template: "{{ is_state('binary_sensor.boneio_32_l_07_73bbd8_in_25_reed_entrance_door', 'off') }}"
    lock:
      action: light.turn_off # lub switch w zależności od mapowania BoneIO
      target:
        entity_id: light.boneio_32_l_07_73bbd8_door_23_relay
    unlock:
      action: input_button.press
      target:
        entity_id: input_button.btn_entrance_door
```
*Dzięki temu polecenie głosowe: "Otwórz drzwi" / "Wpuść gości" wywoła `lock.unlock` lub `button.press`, a "Zgaś wszystkie światła" nigdy nie otworzy drzwi.*

### 5.2. Utworzenie Natywnej Encji Rolety (`cover.template`)
Zastąpienie rozproszonych skryptów jedną encją `cover`:

```yaml
# Propozycja w configuration.yaml (Template Cover dla Sypialni):
cover:
  - platform: template
    covers:
      roleta_sypialnia:
        device_class: shutter
        friendly_name: "Roleta w Sypialni"
        unique_id: roleta_sypialnia_cover
        open_cover:
          action: script.roller_bedroom_on
        close_cover:
          action: script.roller_bedroom_down
        stop_cover:
          action:
            - action: switch.turn_off
              entity_id: switch.roleta_sypialnia_up_relay
            - action: switch.turn_off
              entity_id: switch.roleta_sypialnia_down_relay
```
*Głosowo: "Zasłoń roletę w sypialni", "Odsłoń roletę", "Zatrzymaj roletę".*

### 5.3. Dodanie Brakujących Termostatów `local00` w `mqtt.yaml`
Dodać encje `climate` dla:
1. `local00_kuchnia_termostat`
2. `local00_malalazienka_termostat`
3. `local00_wiatrolap_termostat`
4. `local00_holwejscie_termostat`
5. `local00_holsypialnia_termostat`
6. `local00_gospodarcze_termostat`

Wzorzec:
```yaml
- climate:
    unique_id: local00_kuchnia_termostat
    name: "Kuchnia Termostat"
    mode_state_topic: "local00/Kuchnia/details"
    mode_state_template: "{{ value_json['mode'] if value_json['mode'] is defined else 'heat' }}"
    current_temperature_topic: "local00/Kuchnia/details"
    current_temperature_template: "{{ value_json['temperature'] }}"
    temperature_state_topic: "local00/Kuchnia/details"
    temperature_state_template: "{{ value_json['setpoint'] }}"
    temperature_command_topic: "local00/thermostat/Kuchnia/temperature/set"
    temp_step: 0.5
    modes:
      - 'off'
      - 'heat'
```

### 5.4. Deklaracja Wszystkich Brakujących Pomocników w `configuration.yaml`
```yaml
input_datetime:
  pora_nocna:
    name: Pora Nocna
    has_date: false
    has_time: true
    initial: "23:00:00"
  pora_pobudka:
    name: Pora Pobudki (Dni robocze)
    has_date: false
    has_time: true
    initial: "06:45:00"
  pora_pobudka_weekend:
    name: Pora Pobudki (Weekend)
    has_date: false
    has_time: true
    initial: "08:30:00"
  pora_switu:
    name: Pora Świtu
    has_date: false
    has_time: true
    initial: "06:00:00"
  pora_zmroku:
    name: Pora Zmroku
    has_date: false
    has_time: true
    initial: "18:00:00"
  pora_zmroku_na_zewnatrz:
    name: Pora Zmroku na Zewnątrz
    has_date: false
    has_time: true
    initial: "17:30:00"

input_select:
  shelly_mode:
    name: Tryb Shelly LED Salon
    options:
      - 'Off'
      - 'White'
      - 'Animation'
      - 'Cinema'
    initial: 'Off'
  shelly_animation_type:
    name: Typ Animacji Shelly
    options:
      - cycle
      - wave
      - chase
      - pulse
      - rock
    initial: cycle

input_number:
  shelly_brightness:
    name: Shelly Jasność
    min: 1
    max: 100
    step: 1
    initial: 50
  kolor_bialy_salon:
    name: Salon Balans Bieli
    min: 0
    max: 255
    step: 5
    initial: 120

input_button:
  btn_entrance_door:
    name: Otwórz Drzwi Wejściowe
    icon: mdi:door-open
  roleta_sypialnia_up:
    name: Roleta Góra
  roleta_sypialnia_down:
    name: Roleta Dół
  shelly_animation_toggle:
    name: Zmień Animację Shelly
```

### 5.5. Zdefiniowanie Struktury Obszarów (Areas Hierarchy)
Wprowadzić w Home Assistant jednoznaczne strefy:
- **Parter:** Wiatrołap, Hol Wejściowy, Kuchnia, Jadalnia, Salon, Mała Łazienka, Gabinet, Pomieszczenie Gospodarcze.
- **Piętro:** Korytarz Piętro, Sypialnia, Pokój Klary, Pokój Nikoli, Duża Łazienka, Pralnia.
- **Zewnętrzne:** Podjazd, Taras, Ogród.

Każdy sterownik, światło i czujnik PIR/kontaktron musi zostać przypisany do swojego obszaru.

---

## 6. Pakiet Usprawnień 2: Konsolidacja i Naprawa Automatyzacji

### 6.1. Konsolidacja Automatyzacji Małej Łazienki
Połączyć 6 konkurujących automatyzacji w **jedną, odporną na wyścigi regułę** z obsługą blokady głosowej:

```yaml
# Koncepcja: Jednolita automatyzacja Małej Łazienki
alias: "Strefa: Mała Łazienka — Inteligentne Oświetlenie"
id: mala_lazienka_smart_lighting
mode: restart
trigger:
  - trigger: state
    entity_id: binary_sensor.boneio_32_l_07_73bbd8_in_29_pir_small_bathroom
    to: 'on'
    id: pir_ruch
  - trigger: state
    entity_id: binary_sensor.kontaktron_mala_lazienka
    to: 'on'
    id: drzwi_otwarte
  - trigger: state
    entity_id: binary_sensor.boneio_32_l_07_73bbd8_in_29_pir_small_bathroom
    to: 'off'
    for:
      minutes: 2
    id: brak_ruchu
condition:
  - condition: state
    entity_id: input_boolean.motion_lights_disabled
    state: 'off'
action:
  - choose:
      - conditions:
          - condition: trigger
            id: [pir_ruch, drzwi_otwarte]
        sequence:
          - action: light.turn_on
            target:
              entity_id: light.boneio_32_l_07_new_light_20
      - conditions:
          - condition: trigger
            id: brak_ruchu
          - condition: state
            entity_id: binary_sensor.kontaktron_mala_lazienka
            state: 'off' # Jeśli drzwi zamknięte i brak ruchu, zgaś
        sequence:
          - action: light.turn_off
            target:
              entity_id: light.boneio_32_l_07_new_light_20
```

### 6.2. Naprawa Skryptu `all_lights_off`
Zastąpienie nieistniejących `floor_id` i `label_id` jawną grupą lub poprawnie przypisanymi obszarami:

```yaml
all_lights_off:
  alias: Wszystkie Światła — wyłącz
  icon: mdi:lightbulb-off
  sequence:
    - action: light.turn_off
      target:
        entity_id:
          - light.salon_mqtt
          - light.boneio_32_l_07_new_light_18
          - light.shellyrgbw2_salon
          - light.boneio_32_l_07_new_light_17
          - light.boneio_dr_8ch_03_2c7fbc_chl_04
          - light.boneio_dr_8ch_03_2c7fbc_chr_04
          - light.boneio_32_l_07_new_light_05
          - light.boneio_32_l_07_new_light_06
          - light.boneio_32_l_07_new_light_01
          - light.boneio_dr_8ch_03_2c7fbc_chl_01
          - light.boneio_dr_8ch_03_2c7fbc_chl_02
          - light.boneio_32_l_07_new_light_16
          - light.boneio_32_l_07_new_light_14
          - light.boneio_32_l_07_new_light_10
          - light.boneio_32_l_07_new_light_19
          - light.boneio_32_l_07_new_light_20
          - light.boneio_32_l_07_new_light_09
    - action: input_select.select_option
      data:
        option: 'Off'
      target:
        entity_id: input_select.shelly_mode
```

### 6.3. Unifikacja Kalendarza Odpadów
W `templates.yaml` naprawić błąd braku domyślnej wartości (`default=0` dla `as_timestamp`) oraz ujednolicić encję kalendarza:
```jinja2
# Bezpieczny szablon w templates.yaml:
state: >
  {% set start = state_attr('calendar.smieci_i_alarmy', 'start_time') %}
  {% if start %}
    {{ ((as_timestamp(start, 0) - as_timestamp(now(), 0)) / 86400) | int + 1 }}
  {% else %}
    unavailable
  {% endif %}
```
Zmienić w automatyzacji powiadomienia `calendar.odpady` na `calendar.smieci_i_alarmy`.

### 6.4. Poprawka Ładowania Tesli
W automatyzacji `Tesla Charging Best Window`:
- Poprawić `switch.tesl_y_charge` na poprawną nazwę encji (np. `switch.tesla_y_charge`),
- Usunąć błędny warunek `state: []` na `condition: state, state: 'home'` dla trackera lokalizacji.

---

## 7. Pakiet Usprawnień 3: Projekt Interakcji Głosowych i Nowe Makra/Sceny

### 7.1. Słownik Podstawowych Poleceń Głosowych (Natural Polish Voice Schema)

| Kategoria | Przykładowa komenda głosowa | Wywoływana akcja w HA | Odpowiedź głosowa asystenta |
|---|---|---|---|
| **Światła** | *"Zgaś światło"* *(będąc w kuchni)* | `light.turn_off` (obszar: Kuchnia) | *"Wyłączono światło w kuchni"* |
| **Światła** | *"Zrób nastrojowe światło w salonie"* | `scene.salon_relaks` | *"Ustawiono oświetlenie relaksacyjne"* |
| **Światła** | *"Wyłącz wszystkie światła"* | `script.all_lights_off` | *"Wszystkie światła zostały zgaszone"* |
| **Rolety** | *"Zasłoń roletę w sypialni"* | `cover.close_cover` | *"Zasłaniam roletę"* |
| **Rolety** | *"Uchyl roletę w sypialni"* | `script.roller_bedroom_up_1_5` | *"Roleta uchylona"* |
| **Klimat** | *"Jaka jest temperatura w salonie?"* | Odczyt `climate.local00_salon_termostat` | *"W salonie jest 21,5 stopnia, nastawa to 22"* |
| **Klimat** | *"Ustaw 22 stopnie u Klary"* | `climate.set_temperature` (22.0°C) | *"Ustawiono 22 stopnie w pokoju Klary"* |
| **Klimat** | *"Jest mi za zimno w sypialni"* | `climate.set_temperature` (+1.0°C) | *"Podnoszę temperaturę w sypialni o 1 stopień"* |
| **Brama i Drzwi** | *"Otwórz bramę"* | `cover.open_cover` (`cover.brama_wynajem_zaslona`) | *"Otwieram bramę"* |
| **Brama i Drzwi** | *"Otwórz furtkę / rygiel"* | `button.press` (`input_button.btn_entrance_door`) | *"Rygiel otwarty"* |
| **Energia** | *"Kiedy jest najtańszy prąd?"* | Odczyt `sensor.pstryk_best_window_dol` | *"Najtańszy prąd będzie dzisiaj między 13:00 a 16:00, średnio 0,42 zł za kWh"* |
| **Energia** | *"Czy opłaca się teraz włączyć pralkę?"* | Odczyt `binary_sensor.pstryk_in_best_window_dol` | *"Tak, jesteśmy w najtańszym oknie cenowym"* lub *"Nie, taniej będzie od godziny 13:00"* |
| **Odpady** | *"Jakie śmieci jutro wystawić?"* | Odczyt `sensor.nastepny_wywoz_odpadow` | *"Jutro odbiór odpadów: Plastik i metale"* |
| **Bezpieczeństwo** | *"Czy dom jest zamknięty?"* | Sprawdzenie sensorów drzwi/okien | *"Wszystkie drzwi są zamknięte"* lub *"Uwaga, otwarte jest okno w gabinecie"* |

---

### 7.2. Zestaw Nowych Makro-Scen Głosowych

#### Scena 1: *"Dobranoc"* (`script.voice_goodnight`)
Użytkownik kładzie się spać i mówi: *"Hej dom, dobranoc"*.
1. Wyłącza wszystkie światła wewnętrzne w całym domu.
2. Zamyka roletę w sypialni (`cover.close_cover`).
3. Wyłącza telewizor / media playery w salonie i sypialni.
4. Sprawdza stan kontaktronów (drzwi wejściowe, taras, okna). Jeśli któreś są otwarte, generuje komunikat TTS: *"Uwaga, okno w gabinecie jest nadal otwarte"*.
5. Przestawia termostaty w sypialniach na temperaturę nocną (np. 19.5°C).
6. Uzbraja alarm w trybie domowym (`alarm_control_panel.alarm_arm_home`).
7. Odpowiada cichym komunikatem: *"Dobranoc, dom zabezpieczony"*.

#### Scena 2: *"Dzień dobry"* (`script.voice_goodmorning`)
1. Podnosi roletę w sypialni (`cover.open_cover`).
2. Rozbraja alarm nocny.
3. Przestawia termostaty na tryb dzienny (komfort).
4. Asystent odtwarza poranny brief głosowy:
   - Aktualna pogoda i prognoza na dzień,
   - Informacja o najtańszych godzinach prądu wg Pstryk,
   - Przypomnienie o wywozie śmieci (jeśli dzisiaj lub jutro).

#### Scena 3: *"Wychodzę z domu"* (`script.voice_leaving_home`)
1. Wyłącza wszystkie światła, listwy LED Shelly i sprzęty RTV.
2. Sprawdza zamknięcie drzwi i okien.
3. Zmniejsza temperaturę ogrzewania o 1.5°C (tryb eko).
4. Aktywuje 30-sekundowe opóźnienie, po czym uzbraja pełny alarm wyjściowy (`alarm_arm_away`).
5. Upewnia się, że brama wjazdowa jest zamknięta.

#### Scena 4: *"Tryb Kino"* (`scene.kino_salon` oraz `scene.kino_sypialnia`)
1. Domyka roletę (w sypialni).
2. Wyłącza główne oświetlenie sufitowe.
3. Włącza oświetlenie LED z jasnością 10% w ciepłym odcieniu.
4. Włącza TV i ustawia odpowiednie wejście/aplikację.

#### Scena 5: *"Wietrzenie"* (`script.voice_ventilation_pause`)
Użytkownik mówi: *"Wietrzę sypialnię"* lub *"Wietrzę salon"*.
1. System wyłącza ogrzewanie w danym pokoju na 30 minut (lub obniża zadaną temperaturę do 12°C).
2. Po 30 minutach automatycznie przywraca poprzednią zadaną temperaturę i powiadamia krótkim sygnałem dźwiękowym lub komunikatem TTS.

---

## 8. Pakiet Usprawnień 4: Architektura Silnika Voice AI (Assist / Gemini / Whisper)

### 8.1. Trzywarstwowa Architektura Asystenta Hybrydowego

```
               [ Polecenie Głosowe Użytkownika ]
                              │
                              ▼
            [ STT: Wyoming Whisper / Faster-Whisper ]
                              │
                              ▼
        ┌──────────────────────────────────────────────┐
        │       Dopasowanie Intencji (Routing)         │
        └──────────────────────┬───────────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            ▼                                     ▼
   [ Warstwa 1: Szybkie Intencje ]     [ Warstwa 2: Model AI LLM ]
   (Built-in Assist / Custom YAML)     (Google Gemini Conversation)
   - Światła (Włącz / Wyłącz)          - Podsumowanie stanu domu
   - Rolety (Otwórz / Zamknij)         - Porównanie cen prądu
   - Proste nastawy temperatur         - Złożone pytania logiczne
   - Czas reakcji: < 300 ms            - Czas reakcji: 1.0 - 1.8 s
            │                                     │
            └──────────────────┬──────────────────┘
                               │
                               ▼
              [ Wykonanie Akcji w Home Assistant ]
                               │
                               ▼
                [ TTS: Wyoming Piper (Język PL) ]
```

1. **Warstwa 1 (Local Intents – szybkie akcje domowe):**
   - Zdefiniowana w `config/custom_sentences/pl/*.yaml`.
   - Błyskawiczny czas reakcji (poniżej 300 ms), brak konieczności łączenia z internetem.
   - Odpowiedzialna za proste komendy operacyjne: światło, roleta, brama, rygiel.

2. **Warstwa 2 (Generative AI – Google Gemini / OpenAI):**
   - Integracja `google_generative_ai_conversation` już częściowo istnieje w projekcie (była testowana przy analizie obrazu z kamer).
   - Skonfigurowanie Gemini jako agenta konwersacyjnego z obsługą **Home Assistant Tools (Function Calling)**.
   - LLM otrzymuje dostęp wyłącznie do wyselekcjonowanych encji domowych (bez technicznych sensorów ESP32 i lokali 01–06).
   - Obsługuje pytania otwarte: *"Które okna są otwarte?", "O której godzinie najlepiej włączyć zmywarkę?", "Dlaczego grzejnik w sypialni jest włączony?"*.

3. **Warstwa Bezpieczeństwa (Security Gate):**
   - Operacje wrażliwe: Rozbrojenie alarmu (`alarm_disarm`) oraz otwarcie rygla drzwi (`unlock`) wymagają **autoryzacji kodem PIN** wypowiadanym głosem lub potwierdzenia w aplikacji mobilnej. Zapobiega to wydaniu polecenia otwarcia drzwi przez osobę stojącą za oknem.

---

## 9. Matryca Wdrożenia i Kolejność Prac

Wdrażanie należy podzielić na 4 logiczne, izolowane etapy, aby nie zakłócić bieżącego funkcjonowania domu:

| Faza | Zakres zadań | Szacowany nakład | Ryzyko |
|---|---|---|---|
| **Faza 1: Bezpieczeństwo i Encje** | • Konwersja rygla `door_23_relay` z `light` na `lock`/`button`<br>• Utworzenie encji `cover` dla rolety sypialni<br>• Dodanie brakujących 6 encji `climate` w `local00`<br>• Deklaracja brakujących pomocników (`input_datetime`, `input_select`) | 2–3 h | Niskie (poprawia stabilność) |
| **Faza 2: Porządki w Automatyzacjach** | • Konsolidacja 6 automatyzacji małej łazienki w jedną<br>• Usunięcie duplikatów automatyzacji świtu i wschodu słońca<br>• Przepisanie `all_lights_off` z usunięciem nieistniejących floor/label<br>• Poprawa encji kalendarza odpadów i ładowarki Tesli | 2–4 h | Niskie (eliminuje błędy w logach) |
| **Faza 3: Struktura Voice i Obszary** | • Utworzenie rejestru Obszarów (Areas) w HA i przypisanie urządzeń<br>• Konfiguracja listy encji widocznych dla głosu (`expose_to_assist`)<br>• Wykluczenie mieszkań na wynajem z asystenta domowego<br>• Przełączenie języka pipeline Assist na `pl` | 1–2 h | Zerowe |
| **Faza 4: Nowe Sceny i Silnik AI** | • Wdrożenie makr głosowych (*Dobranoc*, *Dzień dobry*, *Wychodzę*, *Wietrzenie*)<br>• Podpięcie Google Gemini jako konwersacyjnego agenta fallback<br>• Opracowanie zestawu polskich zdań w `custom_sentences/pl/`<br>• Testy z satelitami głosowymi (telefon, tablet, ESP32 Box) | 3–5 h | Niskie |

---

> **Zalecenie końcowe:** Niniejszy dokument stanowi kompletny plan działania. Wszystkie proponowane zmiany zachowują zgodność z magistralą MQTT sterowników ESP32 oraz istniejącymi pulpitami Lovelace, jednocześnie eliminując dług techniczny i otwierając system na bezproblemowe sterowanie głosem.
