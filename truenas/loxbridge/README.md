# LoxBridge

[LoxBridge](https://github.com/andileeb/lox-bridge) backs up the SD card of a Loxone Miniserver Gen 2 to local
storage and any number of S3 targets on a schedule, and forwards live values read-only to InfluxDB, Prometheus,
MQTT (with Home Assistant discovery) and webhooks.

Open the Web UI after installing: the first visit sets the admin password, then **Get started** finds the
Miniserver on your network and runs the first backup.

Back up the Config storage (or set `SECRET_KEY` as an additional environment variable): it holds the key that
encrypts the stored Miniserver and S3 passwords.
