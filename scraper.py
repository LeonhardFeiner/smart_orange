import os
import threading
import time
import json
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException
from paho.mqtt import Client as MqttClient

SELENIUM_REMOTE_URL = os.getenv("SELENIUM_REMOTE_URL", "http://chromium:4444/wd/hub")
MQTT_BROKER = os.getenv("MQTT_BROKER", "mqtt://localhost")
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_CLIENT_ID = "home-scraper"
MQTT_TOPIC_DATA = "home/scraper/data/latest"
MQTT_TOPIC_COMMAND = "home/scraper/commands/set_mode"
QUEUED_INTERVAL = int(os.getenv("PUBLISH_INTERVAL", "60")) # seconds


def fix_mojibake(s):
    try:
       return s.encode('latin1').decode('utf8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        print(f"Failed to fix mojibake for string: {s}")
        return s


def get_selected_and_available_modes(driver):
    selected_mode = None
    try:
        selected_mode_elem = driver.find_element(By.CSS_SELECTOR, 'div[id$="ModusText"]')
        selected_mode = selected_mode_elem.text.strip()
    except NoSuchElementException:
        # Element not present on this page, no selected mode available
        selected_mode = None

    available_modes = []
    try:
        current_mode_btn = driver.find_element(By.CSS_SELECTOR, 'div[id^="bObj_"][onclick^="clickMode"]')
        current_mode_btn.click()

        WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, 'div[onclick^="setOperationMode"]'))
        )
        mode_buttons = driver.find_elements(By.CSS_SELECTOR, 'div[onclick^="setOperationMode"]')

        for btn in mode_buttons:
            text_elem = btn.find_element(By.CSS_SELECTOR, 'div[class*="Text"]')
            available_modes.append(text_elem.text.strip())

        back_btn = driver.find_element(By.ID, "btnBackHK")
        back_btn.click()

        WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.ID, "headerText"))
        )
    except Exception as e:
        print(f"Error fetching available modes: {e}")

    return selected_mode, available_modes


def scrape():
    options = Options()
    options.add_argument("--headless")
    driver = webdriver.Remote(command_executor=SELENIUM_REMOTE_URL, options=options)
    url = "http://192.168.0.9/"
    print(f"Opening URL: {url}", flush=True)
    driver.get(url)


    time.sleep(1)  # Allow DOM stabilization after page change

    data = {}

    while True:
        WebDriverWait(driver, 15).until(EC.visibility_of_element_located((By.ID, "headerText")))
        header = str(driver.find_element(By.ID, "headerText").text.strip())
        print(f"Current header: {header}", flush=True)

        if header in data:
            print(f"Returned to first header {header} after full cycle, stopping.", flush=True)
            break
        else:
            print(f"{header} not seen in before in {data.keys()}, extracting data.", flush=True)


        page_data = {}

        for container_class in ["processScreenListTop", "processScreenListMiddle", "processScreenListBottom"]:
            containers = driver.find_elements(By.CSS_SELECTOR, f".{container_class}")
            for c in containers:
                label_el = c.find_element(By.CSS_SELECTOR, ".processScreenLabel")
                value_el = c.find_element(By.CSS_SELECTOR, ".processScreenValue")
                label = label_el.text.strip()
                value = value_el.text.strip()
                page_data[label] = value

        # Add message(s) to dict
        message_elements = driver.find_elements(By.XPATH, "//*[starts-with(@id, 'Message')]")
        for i, msg_el in enumerate(message_elements):
            try:
                message_text = msg_el.text.strip()
                key = msg_el.get_attribute("id") or f"Message_{i}"
                if message_text:
                    page_data["Message"] = message_text
            except Exception as e:
                print(f"Error extracting message: {e}", flush=True)

        mode, available_modes = get_selected_and_available_modes(driver)
        if mode:
            page_data["Mode"] = mode
            page_data["Available Modes"] = available_modes

        print(f"Extracted data for '{header}': {page_data}", flush=True)
        data[header] = page_data

        down_buttons = driver.find_elements(By.XPATH, "//div[starts-with(@onclick, 'javascript:clickBtnDown')]")
        clickable_buttons = [btn for btn in down_buttons if btn.is_displayed() and btn.is_enabled()]

        if not clickable_buttons:
            print("No clickable 'Down' button found, stopping.", flush=True)
            break

        btn_to_click = clickable_buttons[0]
        print(f"Clicking button with onclick: {btn_to_click.get_attribute('onclick')}", flush=True)
        btn_to_click.click()

        WebDriverWait(driver, 15).until(
            lambda d: d.find_element(By.ID, "headerText").text.strip() != header
        )
        time.sleep(1)  # Allow DOM stabilization after page change

    # After finishing page loop but before returning data:

    common_data = {}

    try:
        temp_c = driver.find_element(By.ID, "tempC").text.strip()
        time_c = driver.find_element(By.ID, "timeC").text.strip()
        logout_el = driver.find_element(By.CSS_SELECTOR, "a[href='../index.php?logout=true'] span.gray")
        common_data["Aussentemperatur"] = temp_c
        common_data["Uhrzeit"] = time_c.replace("Uhr", "").strip()
        print(f"Extracted common data: {common_data}", flush=True)
    except Exception as e:
        print(f"Error extracting common data: {e}", flush=True)

    data["common"] = common_data


    driver.quit()
    print("Scraping complete.", flush=True)
    return data

def parse(result):
    def fix_name(s):
        return (
            fix_mojibake(s)
            .replace(" ", "")
            .replace("-", "_")
            .replace("ä", "ae")
            .replace("ö", "oe")
            .replace("ü", "ue")
            .replace("ß", "ss")
            .replace("(", "")
            .replace(")", "")
            .replace("/", "_")
            .replace("&", "Und")
        )
    new_result = {}
    for page, page_data in result.items():
        new_page_data = {}
        new_page = fix_name(page)
        for key, value in page_data.items():
            new_key = fix_name(key)
            new_value = value
            if isinstance(value, str):
                new_value = fix_mojibake(value).lower()
                if new_value.endswith("°c"):
                    new_value = new_value.replace("°c", "").strip()
                if new_value.endswith("l/min"):
                    new_value = new_value.replace("l/min", "").strip()

                try :
                    if '.' in new_value:
                        new_value = float(new_value)
                    else:
                        new_value = int(new_value)
                except ValueError:
                    pass

            new_page_data[new_key] = new_value
        new_result[new_page] = new_page_data
                
    return new_result


class MqttBridge:
    def init(self):
        self.client = MqttClient(MQTT_CLIENT_ID)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.connected = False
        self.lock = threading.Lock()

    def connect(self):
        # If broker requires auth, provide username/password via username_pw_set
        if MQTT_USERNAME and MQTT_PASSWORD:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.client.connect("localhost", 1883, 60)  # adjust host/port as needed
        self.client.loop_start()

    def on_connect(self, client, userdata, flags, rc):
        self.connected = True
        print("MQTT connected", rc)

    def on_disconnect(self, client, userdata, rc):
        self.connected = False
        print("MQTT disconnected", rc)

    def publish(self, topic, payload):
        with self.lock:
            if self.connected:
                self.client.publish(topic, json.dumps(payload))
            else:
                print("MQTT not connected, skip publish")

def main_loop():
    mqtt_bridge = MqttBridge()
    mqtt_bridge.connect()
    try:
        while True:
            result = scrape()
            parsed = parse(result)
            mqtt_bridge.publish(MQTT_TOPIC_DATA, parsed)
            time.sleep(QUEUED_INTERVAL)
    finally:
        mqtt_bridge.client.loop_stop()
        mqtt_bridge.client.disconnect()

if __name__ == "__main__":
    main_loop()