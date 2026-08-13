"""WiFi credentials template.

Copy this to `secrets.py` and fill in your network. `secrets.py` is
gitignored, so your credentials never reach a commit.

    cp secrets.example.py secrets.py

Then copy it to the board alongside the smoke test:

    python tools/sync_board.py     # copies secrets.py as secrets.py

Edit the values below before running anything - leaving the placeholders in
place makes the WiFi stages fail with "no such network in range".
"""

WIFI_SSID = "your-network-name"
WIFI_PASSWORD = "your-network-password"

# Optional. Sets the DHCP hostname the board announces on your LAN, which
# makes it easier to find in your router's client list. Leave as None to
# keep the MicroPython default.
HOSTNAME = "pico2w"

# The machine running the sender application, on that network. Only used by
# the WiFi transport below. Its absence here once cost an evening: the pendant
# came up, joined, and then failed in a way that read as a network fault
# rather than a missing setting.
SENDER_HOST = "192.168.1.100"
SENDER_PORT = 8422

# Which transport the pendant uses.
#
# False joins the WiFi above and opens a TCP session to SENDER_HOST. True
# ignores both and talks ESP-NOW to the receiver board plugged into the
# sender's PC, discovering it by broadcast - so nothing above needs to be
# right for that path, and nothing changes when the PC's address does.
#
# ESP-NOW is where this is going: an open-source pendant cannot require the
# builder to have usable WiFi in their shop. The WiFi path stays because it
# is the reference every jog constant was fitted against.
USE_ESPNOW = False
