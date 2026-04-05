/*
 * ESP32 LED blinker for camera latency measurement
 *
 * Blinks LED_PIN at a known duty cycle:
 *   ON_MS  = 500ms
 *   OFF_MS = 2000ms
 *
 * The asymmetric timing makes it easy to distinguish ON→OFF from OFF→ON.
 * Wire LED to LED_PIN with a 220Ω resistor to GND.
 *
 * Board: ESP32 Dev Module
 */

#define LED_PIN   2      // Built-in LED on most ESP32 dev boards
#define ON_MS    500
#define OFF_MS  2000

void setup() {
  pinMode(LED_PIN, OUTPUT);
  Serial.begin(115200);
  Serial.println("Camera latency blinker started");
}

void loop() {
  digitalWrite(LED_PIN, HIGH);
  Serial.println("ON");
  delay(ON_MS);

  digitalWrite(LED_PIN, LOW);
  Serial.println("OFF");
  delay(OFF_MS);
}
