# GPIO command notifications on Seeeduino Cloud v1.0

This profile uses the connections in Seeed's published
[v1.0 schematic](https://files.seeedstudio.com/wiki/Seeeduino_Cloud/res/Seeeduino_Cloud_PDF.pdf)
and [Eagle source](https://files.seeedstudio.com/wiki/Seeeduino_Cloud/res/Seeeduino_Cloud_v1.0.zip).
It does not use D7: although the board guide describes a D7 handshake, the
published Seeeduino schematic connects D7 only to the AVR and header.

| Purpose | Linux GPIO | Board path | AVR |
| --- | --- | --- | --- |
| Command pending, active low | 21 | U8 A1/B1, TXS0102 voltage translator | PB2 / D16 / MOSI / PCINT2 |
| Translator enable | 19 | U5/U8 OE | Shared SPI translator control |

No extra wire is expected for this board revision. The AVR remains an input with
its internal pull-up enabled. Linux initializes GPIO21 high before enabling the
translator and holds it low only while a confirmed command is pending. The
pull-up keeps the input inactive when Linux releases it or disables the
translator. The [TXS0102 datasheet](https://www.ti.com/lit/ds/symlink/txs0102.pdf)
describes its voltage domains, internal pull-ups, and high-impedance disable state.
These pin numbers are board-specific; the original Arduino Yun has different
interprocessor wiring despite using the same PlatformIO AVR target.

## Command flow

1. The existing command processor validates the session/deadline and durably
   journals physical actions before dispatch.
2. The agent exclusively claims GPIO21 and initializes it inactive. It claims
   GPIO19, or preserves an already enabled high output without taking ownership.
   It refuses foreign GPIO21 exports, a disabled exported GPIO19, and kernel
   ownership; it never unbinds a driver or steals a pin.
3. It queues the atomic `resac_command` write on the persistent Bridge socket.
   The notification is asserted only when the exact command frame is echoed in
   Bridge's `put` confirmation, before the command's execution deadline.
4. The AVR's `PCINT0` ISR records a flag. The main loop checks the flag and held
   input, then reads Bridge once and runs the existing command handler. No
   Bridge calls or IR work run in the interrupt. Checking the held input catches
   an assertion during a DHT section that masks interrupts.
5. The matching AVR result wakes Linux through the existing Mailbox stream.
   Validated completion releases the GPIO before persisting/reporting the result.
   Duplicate IDs still return their retained result without repeating IR work.

There is no periodic command datastore read in the default sketch. A held
notification causes one read, rather than another read on each loop iteration.
The existing 250 ms configuration/watchdog housekeeping, sensor schedule, IR
dwell, freshness guards, and command deadline remain. An ongoing blocking sensor
read or IR transmission still delays main-loop command handling.

The firmware advertises `resac_command_notify=gpio21-pb2-v1`. The agent touches
GPIO only for that exact capability. Older event firmware without the capability
keeps its existing polling handler; unknown nonempty profiles are rejected.
Building with `-DRESAC_COMMAND_GPIO=0` restores the old 20 ms command handler and
advertises no GPIO capability, for a coordinated rollback or another board.

## Linux Bridge readiness

The legacy installed `PacketReader.t_read()` waits on the UART for up to 50 ms.
Its Mailbox TCP service previously ran between those waits; frequent AVR reads
also happened to wake it. Removing those reads alone could therefore increase
command latency.

`bridge_socket_wait.py` includes Mailbox listener/client read readiness and queued
TCP response write readiness in that wait. It also drains already decoded JSON
commands. TCP events service the existing runners immediately, while each UART
byte retains its original timeout. A TCP event does not return a false UART
timeout midway through a frame. The 50 ms timeout remains for the existing
background runners; command delivery and put confirmation do not depend on it.

The installer downloads `bridge/packet.py` and matches the exact known
`t_read()` implementation before preparing its replacement. An unrecognized
implementation aborts before stopping the existing agent. Both Python files are
compiled with the board's interpreter during staging. Installation preserves
`packet.py.res-ac-original`, adds the small helper beside it, atomically replaces
the reader, and removes stale bytecode. The running Bridge process is not killed;
the update takes effect when Bridge restarts with the newly flashed AVR sketch.

`scripts/prepare_bridge_socket_wait.py SOURCE OUTPUT` prepares the same narrow
replacement locally without deploying it. Restoring the saved original reader,
removing its bytecode, and restarting Bridge reverses this service change.

## Ownership, failure, and recovery

`fcntl.flock` on `/var/run/res-ac-command-gpio.lock` prevents concurrent agents.
The RAM-only record identifies pins exported by this feature. After a process
crash, a new lock owner can reclaim only those recorded pins, first releasing
any stale assertion. Normal shutdown, Bridge disconnect/re-handshake, and AVR
restart discard pending notifications and release owned pins. Existing enabled
GPIO19 outputs remain untouched on cleanup.

Expired or canceled commands cannot be asserted by a late Bridge confirmation.
Signal failures disconnect the stream and discard queued actions. Missing
acknowledgements retain the existing uncertain-result behavior; a GPIO signal is
not evidence that an IR action ran, and a restarted process never automatically
replays an uncertain physical action.

**Stop the agent before AVR programming through Linux's shared SPI interface.**
The saved board `avrdude.conf` assigns MOSI to GPIO21, SCK to GPIO18, and MISO to
GPIO22. Firmware programming and command notification cannot own that interface
at the same time. Kernel-owned GPIOs must be investigated before deployment;
the implementation does not unload SPI drivers or alter pin multiplexing.

`python res_ac_agent.py --gpio-preflight` reads prerequisites and existing
ownership without changing GPIO outputs or loading device credentials. It checks
sysfs access, `fcntl.flock`, exported pins, and kernel reservations when debugfs
is available. Actual exclusive export remains the final runtime ownership check.
The installer runs this preflight before replacing the existing agent.

## Validation status

Local tests exercise the actual sketch with virtual GPIO registers and clock,
including idle/held/released signals, masked interrupts, cancellation, boot reset,
duplicate IDs, safety guards, rollover, and physical IR dwell. Python tests cover
exact socket confirmation, deadlines, ownership conflicts, crash recovery,
read-only preflight, staged installation, and UART/TCP readiness interactions.
Both GPIO and compatibility firmware paths compile in native tests; the GPIO
sketch also builds with the AVR toolchain.

No live GPIO was driven and no firmware was flashed during development. Board
revision, actual GPIO ownership/multiplexing, voltage levels, boot/programming
interaction, and command latency still require a separately authorized hardware
test. The implementation does not claim measured hardware latency gains.
