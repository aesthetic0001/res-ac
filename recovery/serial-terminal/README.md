# USB-to-Linux serial terminal recovery firmware

This is the original terminal sketch, kept independently from sensor firmware.
It forwards USB serial to the Linux UART at 250000 baud; monitor USB at 115200.

From the repository root:

```sh
~/.platformio/penv/bin/pio run -d recovery/serial-terminal -t upload
```

This replaces only the AVR sketch. It does not overwrite Linux, Wi-Fi settings,
or the private Linux agent configuration. Once uploaded, open the serial monitor
and press Enter to get the Linux console. The reporting firmware's USB output is
sensor/upload diagnostics rather than a Linux shell.

To switch back to reporting firmware from the repository root:

```sh
~/.platformio/penv/bin/pio run -t upload
```

The Linux Bridge process should exit when the reporting firmware is replaced;
if it is still holding the UART, the terminal sketch's documented `~~` sequence
asks Bridge to shut down. Retain this recovery project when deploying the device.
