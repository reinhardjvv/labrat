# Roadmap

These are the next pieces of LABRAT, roughly in order. Ideas and offers of help are
welcome in [Discussions](https://github.com/reinhardjvv/labrat/discussions).

## Next

- **Dashboard.** A web interface on top of the message API, served by the nest: devices, live streams, locks,
  the audit log and a stop button.
- **Rat configuration from the nest.** Edit a rat's devices from the nest and push them, with the rat checking
  every change before taking it.
- **Approvals.** Chosen actions, such as switching a laser on, wait for a person to approve them, whoever asks.
- **More drivers.** Modbus TCP and RTU, VISA through PyVISA, I2C and SPI sensors, stepper and servo motion
  controllers, and the Allied Vision Vimba X SDK alongside Aravis.
- **Instrument profiles.** Ready-made `scpi` descriptions of common instruments, with ratings from the
  manufacturers' documents.
- **Events.** A stream of changes, locks and stops for dashboards and loggers to follow.

## Later

- Rat firmware for ESP32 boards, in MicroPython and C.
- An MQTT bridge carrying the same messages.
- Recorded runs: settings, data and a description of the set-up, kept together.
- Signed releases and reproducible builds.
