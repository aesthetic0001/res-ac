# Seeeduino Cloud Linux upgrade assessment

Assessment date: 2026-10-30. This is a compatibility assessment, not a flashing
procedure. No replacement Linux image has been approved or flashed.

## Installed system

The inspected board runs Linux 3.8.3, built on August 18, 2014, and identifies its
software as Linino Barrier Breaker (`ar71xx/generic`, board name `linino`). The
kernel reports an AR9330 rev 1, with 64 MiB RAM and 16 MiB SPI flash. The software
model string is `Arduino Yun`; that string alone does not establish electrical
compatibility with every Arduino Yún image.

The existing layout is:

| Region | Flash offset | Size |
| --- | --- | --- |
| U-Boot | `0x000000` | `0x040000` |
| U-Boot environment | `0x040000` | `0x010000` |
| Root filesystem | `0x050000` | `0xe50000` |
| Kernel | `0xea0000` | `0x140000` |
| NVRAM | `0xfe0000` | `0x010000` |
| Wi-Fi calibration / ART | `0xff0000` | `0x010000` |

The overlapping `firmware` partition spans `0x050000` through `0xfe0000`.
The current boot command line explicitly supplies this legacy partition map.

## Candidate images inspected

The official [24.10.8 profiles](https://downloads.openwrt.org/releases/24.10.8/targets/ath79/generic/profiles.json)
and [25.12.6 profiles](https://downloads.openwrt.org/releases/25.12.6/targets/ath79/generic/profiles.json)
contain an `arduino_yun` profile. Neither inspected release profile list contains
a Seeeduino Cloud or Dragino MS14 profile. Definitions in the development source
tree are not proof that a particular stable release ships an image.

The 25.12.6 Yún sysupgrade and initramfs images were downloaded and matched their
published SHA-256 values. The initramfs uImage header/data CRCs were checked and
its embedded device tree was inspected. It shares the aggregate firmware region
above, but uses a kernel-first uImage layout and Yún-specific UART GPIO controls.
The installed system uses a rootfs-first layout. Its old
`platform_check_image` function accepts SquashFS/JFFS2 magic values; the downloaded
modern sysupgrade image starts with a uImage header and fails that format rule.
An updater override would not establish that the boot configuration is correct.

The saved bootloader contains a legacy `bootm 0x9fea0000` string and no discovered
`loadb`, `loadx`, `loady`, or `loads` command strings. This is static inspection,
not a successful live bootloader `help`/`printenv` session. The saved environment
region did not decode as a standard CRC-valid U-Boot environment, so generic
environment-write offsets must not be guessed. A read-only boot-console attempt
did not yield a readable bootloader session; the original Linux rebooted normally.

Seeed's [upgrade guide](https://wiki.seeedstudio.com/Seeeduino_Cloud/) links to
[Dragino's vendor archive](https://www.dragino.com/downloads/downloads/motherboards/ms14/Firmware/Yun/Newest_Firmware/).
It lists a 2017 4.1.2 build and a `yun-2.0.8.rar` upload dated 2021. Live downloads
returned HTTP 406 from the vendor's ModSecurity filter during this assessment.
Their contents, exact hardware compatibility, and fixes have not been verified.

## Application migration requirements

The sensor/ping agent supports Python 3, but the installed Linux Bridge service
uses Python 2.7. The inspected upstream
[YunBridge source](https://github.com/arduino/YunBridge/tree/master/bridge) also
still contains Python 2 syntax. A modern image therefore needs a tested Bridge
port or compatible replacement, correct UART setup, full WPA2-Enterprise support,
CA trust, and the agent's startup/configuration files. Package and RAM/flash
budgets must be checked for the actual assembled image.

## Backups and next validation

A private backup was made of configuration files, the Bridge installation,
bootloader, environment, current firmware, NVRAM, and ART. The five flash-region
backups cover all 16 MiB. Serial transfers were checksum-verified, compressed
files were decompressed and checked for expected sizes, and a SHA-256 manifest
was written locally. Backup directories use mode 700 and files mode 600. These
files contain secrets and belong only under the Git-ignored `private/` directory.

`Console.download()` in [scripts/serial_console.py](scripts/serial_console.py)
provides bounded, acknowledged, checksum-verified reads over the recovery sketch.
It returns bytes to the caller without printing them. It does not flash Linux.

Before an OS upgrade, establish a working bootloader/recovery connection, prepare
the correct boot/partition/UART configuration and dependencies, then RAM-boot a
candidate and verify Ethernet, eduroam, Bridge telemetry, ping, and DHCP renewals.
The manufacturer's documented bootloader recovery uses Ethernet/TFTP. With only
USB available during this assessment, a safe RAM-boot/recovery path for the
candidate has not been established. The board remains on its working Linux
installation and the sensor/ping sketch is restored after inspection.
