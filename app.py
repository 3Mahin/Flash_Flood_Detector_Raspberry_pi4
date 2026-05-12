import board
import busio
import digitalio
import time
import serial
import threading
import adafruit_ssd1306
from PIL import Image, ImageDraw
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# ==========================================
# HARDWARE SETUP
# ==========================================

# 1. OLED Setup (I2C)
i2c = busio.I2C(board.SCL, board.SDA)
oled = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c)
image = Image.new("1", (oled.width, oled.height))
draw = ImageDraw.Draw(image)

# 2. Ultrasonic Setup (HC-SR04)
trig = digitalio.DigitalInOut(board.D23)
trig.direction = digitalio.Direction.OUTPUT
echo = digitalio.DigitalInOut(board.D24)
echo.direction = digitalio.Direction.INPUT

# 3. Active Buzzer Setup
buzzer = digitalio.DigitalInOut(board.D18)
buzzer.direction = digitalio.Direction.OUTPUT

# 4. STM32 Water Sensor Setup (UART/Serial)
try:
    ser = serial.Serial('/dev/serial0', 9600, timeout=0.1)
except Exception as e:
    print(f"Failed to open serial port: {e}")
    ser = None

# 5. HW-103 Rain Sensor Setup (Now on GPIO 6 / Pin 31)
rain_sensor = digitalio.DigitalInOut(board.D6)
rain_sensor.direction = digitalio.Direction.INPUT
rain_sensor.pull = digitalio.Pull.UP  # Software pull-up to keep the signal stable

# ==========================================
# GLOBAL STATE & CALIBRATION
# ==========================================
state = {
    "active_sensor": "ultrasonic",
    "distance": 0.0,
    "water_raw": 0,
    "water_percent": 0,
    "buzzer_active": False,
    "alert_message": "System Normal",
    "is_raining": False
}

# Thresholds
MIN_DISTANCE_CM = 5.0
MIN_DRY, MID_POINT, MAX_WET = 260, 433, 488    
ALARM_THRESHOLD = 80 

# ==========================================
# SENSOR FUNCTIONS
# ==========================================
def get_distance():
    try:
        trig.value = False
        time.sleep(0.01)
        trig.value = True
        time.sleep(0.00001)
        trig.value = False

        timeout = time.time() + 0.1 
        while not echo.value:
            if time.time() > timeout: return None
            pulse_start = time.time()

        while echo.value:
            if time.time() > timeout: return None
            pulse_end = time.time()

        return round(((pulse_end - pulse_start) * 34300) / 2, 1)
    except Exception:
        return None

def get_stm32_serial_data():
    if ser is None: return None
    try:
        if ser.in_waiting > 0:
            line = ser.readline().decode('utf-8').strip()
            if line:
                return int(line)
    except (ValueError, UnicodeDecodeError):
        pass
    return None

def update_oled():
    """Draws the current system state to the physical OLED screen."""
    draw.rectangle((0, 0, 128, 64), outline=0, fill=0)
    
    # Header: Alert Status
    status_text = "!!! FLOOD ALERT !!!" if state["buzzer_active"] else "SYSTEM NORMAL"
    draw.text((0, 0), status_text, fill=255)
    
    # Header: Rain Indicator
    if state["is_raining"]:
        draw.text((100, 0), "[RAIN]", fill=255)
        
    draw.line((0, 12, 128, 12), fill=255)

    # Active Mode Header
    mode_label = "MODE: ULTRASONIC" if state["active_sensor"] == "ultrasonic" else "MODE: STM32 WATER"
    draw.text((5, 15), mode_label, fill=255)

    # Sensor Specific Data
    if state["active_sensor"] == "ultrasonic":
        draw.text((5, 30), f"Dist: {state['distance']} cm", fill=255)
        
        perc = int(max(0, min(100, ((30 - state["distance"]) / 25) * 100)))
        draw.rectangle((10, 48, 118, 58), outline=255, fill=0)
        draw.rectangle((11, 49, 11 + int(106 * (perc / 100)), 57), outline=255, fill=255)
        
    elif state["active_sensor"] == "water":
        draw.text((5, 30), f"Raw: {state['water_raw']} | Lvl: {state['water_percent']}%", fill=255)
        
        draw.rectangle((10, 48, 118, 58), outline=255, fill=0)
        draw.rectangle((11, 49, 11 + int(106 * (state['water_percent'] / 100)), 57), outline=255, fill=255)

    oled.image(image)
    oled.show()

# ==========================================
# MAIN HARDWARE LOOP (Background Thread)
# ==========================================
def hardware_loop():
    while True:
        # HW-103 pulls DO low (False) when wet.
        state["is_raining"] = not rain_sensor.value
        
        if state["active_sensor"] == "ultrasonic":
            dist = get_distance()
            if dist is not None: 
                state["distance"] = dist
                if dist <= MIN_DISTANCE_CM:
                    state["buzzer_active"] = True
                    state["alert_message"] = "FLOOD ALERT: Water Level Critical!"
                else:
                    state["buzzer_active"] = False
                    state["alert_message"] = "Monitoring Distance..."
            
            if ser and ser.in_waiting > 0:
                ser.reset_input_buffer()
        
        elif state["active_sensor"] == "water":
            raw_data = get_stm32_serial_data()
            if raw_data is not None:
                state["water_raw"] = raw_data
                
                if raw_data <= MIN_DRY: pct = 0
                elif raw_data >= MAX_WET: pct = 100
                elif raw_data <= MID_POINT:
                    pct = int(((raw_data - MIN_DRY) / (MID_POINT - MIN_DRY)) * 50)
                else:
                    pct = 50 + int(((raw_data - MID_POINT) / (MAX_WET - MID_POINT)) * 50)
                
                state["water_percent"] = pct

                if pct >= ALARM_THRESHOLD:
                    state["buzzer_active"] = True
                    state["alert_message"] = f"FLOOD ALERT: Contact Detected ({pct}%)!"
                else:
                    state["buzzer_active"] = False
                    state["alert_message"] = f"Monitoring Water ({pct}%)..."

        buzzer.value = state["buzzer_active"]
        update_oled()
        time.sleep(0.05)

threading.Thread(target=hardware_loop, daemon=True).start()

# ==========================================
# FLASK WEB ROUTES
# ==========================================
@app.route('/')
def index(): 
    return render_template('index.html')

@app.route('/api/data')
def api_data(): 
    return jsonify(state)

@app.route('/api/toggle', methods=['POST'])
def api_toggle():
    data = request.json
    if 'sensor' in data and data['sensor'] in ['ultrasonic', 'water']:
        state["active_sensor"] = data['sensor']
        
        state["buzzer_active"] = False 
        buzzer.value = False
        if ser: ser.reset_input_buffer()
            
        return jsonify({"status": "success", "active_sensor": state["active_sensor"]})
    return jsonify({"status": "error"}), 400

if __name__ == '__main__':
    try:
        print("Server starting at http://0.0.0.0:5000")
        app.run(host='0.0.0.0', port=5000, debug=False) 
    except KeyboardInterrupt:
        buzzer.value = False
        if ser: ser.close()
        oled.fill(0)
        oled.show()
        print("\nSystem Shutdown Cleanly.")
