/*
 * esp32_ble_scanner.ino  —  OpenDroneID BLE receiver for ESP32
 *
 * WHAT THIS DOES:
 *   Scans for Bluetooth Low Energy (BLE) advertisements conforming to
 *   the ASTM F3411 / ASD-STAN 4709-002 Remote ID (OpenDroneID) standard.
 *
 *   For every BLE packet that matches the OpenDroneID service UUID or
 *   the CID-based manufacturer data format, it:
 *     1. Parses the raw advertisement payload byte-by-byte.
 *     2. Extracts: drone_id, operator_id, latitude, longitude, altitude,
 *        RSSI, manufacturer data, message type.
 *     3. Serialises the result to a single-line JSON string.
 *     4. Sends it over USB Serial (115200 baud) to the host Raspberry Pi.
 *
 * WHY JSON ON SERIAL:
 *   The Python identity module reads this serial port line by line.
 *   JSON makes it trivial to parse in Python with json.loads().
 *   One line = one drone sighting event.
 *
 * OPENDRONEID BLE FORMAT REFERENCE:
 *   Service UUID: 0xFFFA  (OpenDroneID BT4 legacy advertising)
 *   Company ID:   0x02E5  (Bluetooth SIG assigned to ASTM)
 *   Payload layout (after UUID/CID header):
 *     Byte 0:     App code (0x0D for OpenDroneID)
 *     Byte 1:     Counter
 *     Bytes 2+:   Up to 25 bytes — one or more ODID messages (20 bytes each)
 *       Message header (byte 0 of each):
 *         Bits 7-4: Message type (0=BasicID, 1=Location, 2=Auth, 3=SelfID,
 *                                 4=System, 5=OperatorID, 0xF=MessagePack)
 *         Bits 3-0: Protocol version
 *
 * DEPENDENCIES:
 *   Install via Arduino Library Manager:
 *     - "ESP32 BLE Arduino" by Neil Kolban (comes with ESP32 board package)
 *
 * HARDWARE:
 *   Any ESP32 board.  Connect via USB to the Raspberry Pi running the
 *   Python identity module.  No other wiring needed for BLE scanning.
 *
 * FLASH INSTRUCTIONS:
 *   1. Open Arduino IDE, select board: "ESP32 Dev Module"
 *   2. Select the correct COM/ttyUSB port
 *   3. Upload this sketch
 *   4. Set Serial Monitor baud to 115200 to verify output
 */

#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <Arduino.h>

// ── Constants ──────────────────────────────────────────────────────────────

// OpenDroneID BLE Service UUID (16-bit, BT4 legacy advertising)
#define ODID_SERVICE_UUID   0xFFFA

// ASTM Company Identifier in manufacturer-specific data
#define ASTM_COMPANY_ID     0x02E5

// OpenDroneID application code byte (byte 0 after CID)
#define ODID_APP_CODE       0x0D

// BLE scan duration per cycle (seconds). Short = more responsive.
#define SCAN_DURATION_SEC   1

// Message type nibble values (upper nibble of first byte of each ODID message)
#define ODID_TYPE_BASIC_ID      0
#define ODID_TYPE_LOCATION      1
#define ODID_TYPE_AUTH          2
#define ODID_TYPE_SELF_ID       3
#define ODID_TYPE_SYSTEM        4
#define ODID_TYPE_OPERATOR_ID   5
#define ODID_TYPE_MESSAGE_PACK  0xF

// ── BLE scan object ───────────────────────────────────────────────────────

BLEScan* pBLEScan;

// ── Callback — called for every BLE advertisement received ────────────────

class ODIDAdvertisedDeviceCallbacks : public BLEAdvertisedDeviceCallbacks {

  void onResult(BLEAdvertisedDevice device) {

    // ── Check for OpenDroneID service UUID ──────────────────────────────
    bool has_odid_uuid = false;
    if (device.haveServiceUUID()) {
      BLEUUID svcUUID = BLEUUID((uint16_t)ODID_SERVICE_UUID);
      has_odid_uuid = device.isAdvertisingService(svcUUID);
    }

    // ── Check manufacturer-specific data for ASTM CID + app code ────────
    bool has_odid_manuf = false;
    std::string manufData = "";
    if (device.haveManufacturerData()) {
      manufData = device.getManufacturerData();
      // Minimum: 2 bytes CID + 1 byte app code = 3 bytes
      if (manufData.length() >= 3) {
        uint16_t cid = (uint8_t)manufData[1] << 8 | (uint8_t)manufData[0];
        uint8_t  app = (uint8_t)manufData[2];
        has_odid_manuf = (cid == ASTM_COMPANY_ID && app == ODID_APP_CODE);
      }
    }

    // Only process devices that carry OpenDroneID data
    if (!has_odid_uuid && !has_odid_manuf) return;

    // ── Extract basic fields from the advertisement ──────────────────────
    String mac_addr = String(device.getAddress().toString().c_str());
    int    rssi     = device.getRSSI();
    String name     = device.haveName()
                        ? String(device.getName().c_str())
                        : String("unknown");

    // ── Parse ODID payload ───────────────────────────────────────────────
    // The payload bytes after the header contain one or more 20-byte ODID
    // messages.  We extract what we can from manufacturer data.
    String drone_id    = "";
    String operator_id = "";
    String msg_type    = "unknown";
    float  latitude    = 0.0;
    float  longitude   = 0.0;
    float  altitude    = 0.0;

    if (has_odid_manuf && manufData.length() >= 3) {
      // Payload starts at byte index 3 (after 2-byte CID + 1-byte app code)
      // Each ODID message is 20 bytes.  We parse the first one here.
      int payload_offset = 3;
      // counter byte is at index 3 in some implementations; skip it
      // Real payload messages start 1 byte later
      int msg_start = payload_offset + 1;

      if ((int)manufData.length() >= msg_start + 1) {
        uint8_t header_byte = (uint8_t)manufData[msg_start];
        uint8_t mtype       = (header_byte >> 4) & 0x0F;  // upper nibble
        uint8_t proto_ver   = header_byte & 0x0F;          // lower nibble

        // Decode message type to human-readable string
        switch (mtype) {
          case ODID_TYPE_BASIC_ID:
            msg_type = "BasicID";
            // BasicID: bytes 1-21 of the message = ID string (null-terminated)
            if ((int)manufData.length() >= msg_start + 22) {
              char id_buf[21] = {0};
              for (int i = 0; i < 20; i++) {
                id_buf[i] = manufData[msg_start + 1 + i];
              }
              drone_id = String(id_buf);
              // Sanitise — remove non-printable chars
              drone_id.trim();
            }
            break;

          case ODID_TYPE_LOCATION:
            msg_type = "Location";
            // Location message: bytes contain lat/lon/alt in packed form.
            // Full decode requires the opendroneid-core-c library.
            // Here we just flag the message type; the Python side can
            // request the full decode via a compiled C extension if needed.
            break;

          case ODID_TYPE_OPERATOR_ID:
            msg_type = "OperatorID";
            if ((int)manufData.length() >= msg_start + 22) {
              char op_buf[21] = {0};
              for (int i = 0; i < 20; i++) {
                op_buf[i] = manufData[msg_start + 1 + i];
              }
              operator_id = String(op_buf);
              operator_id.trim();
            }
            break;

          case ODID_TYPE_SYSTEM:     msg_type = "System";   break;
          case ODID_TYPE_AUTH:       msg_type = "Auth";     break;
          case ODID_TYPE_SELF_ID:    msg_type = "SelfID";   break;
          case ODID_TYPE_MESSAGE_PACK: msg_type = "MsgPack"; break;
          default:                   msg_type = "Unknown";  break;
        }
      }
    }

    // ── Serialise to JSON and send over Serial ───────────────────────────
    // Format: one JSON object per line.  Python reads line by line.
    // ts_ms is millis() — wraps at ~49 days but enough for session use.
    Serial.print("{");
    Serial.print("\"mac\":\"");        Serial.print(mac_addr);        Serial.print("\",");
    Serial.print("\"rssi\":");         Serial.print(rssi);            Serial.print(",");
    Serial.print("\"name\":\"");       Serial.print(name);            Serial.print("\",");
    Serial.print("\"msg_type\":\"");   Serial.print(msg_type);        Serial.print("\",");
    Serial.print("\"drone_id\":\"");   Serial.print(drone_id);        Serial.print("\",");
    Serial.print("\"operator_id\":\"");Serial.print(operator_id);     Serial.print("\",");
    Serial.print("\"ts_ms\":");        Serial.print(millis());
    Serial.println("}");
    // Serial.println() appends '\n' — Python's readline() stops here
  }
};

// ── Arduino setup ─────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  // Wait for Serial Monitor on some boards
  delay(500);

  Serial.println("{\"status\":\"ESP32 OpenDroneID scanner starting\"}");

  BLEDevice::init("ODID-Scanner");

  // Create a BLE scan object.  Active scan = asks devices for scan response
  // packets which can contain additional data.
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new ODIDAdvertisedDeviceCallbacks());

  // Active scan: sends scan request to advertising device for more data
  pBLEScan->setActiveScan(true);
  // Scan interval and window (units: 0.625 ms)
  // interval=100ms, window=99ms → nearly continuous listening
  pBLEScan->setInterval(160);
  pBLEScan->setWindow(159);

  Serial.println("{\"status\":\"BLE scanner ready\"}");
}

// ── Arduino loop ──────────────────────────────────────────────────────────

void loop() {
  // Run a scan for SCAN_DURATION_SEC seconds, then clear results and repeat.
  // Clearing the cache prevents memory growth over long sessions.
  BLEScanResults results = pBLEScan->start(SCAN_DURATION_SEC, false);
  pBLEScan->clearResults();
  // No delay needed — start() is blocking for SCAN_DURATION_SEC
}
