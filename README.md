[README.md](https://github.com/user-attachments/files/31664885/README.md)
# Connex-LSL

**Connex-LSL** is an experimental transparent TCP proxy and Lab Streaming Layer (LSL) bridge for Xltek/Natus Connex-based EEG systems. This includes breakouts 
REF 10395 and REF 012378 and base units REF 10396 (Connex) and REF 10388 (Brain Monitor). REF 10396 and REF 10388 bases, as well as the associated REF 10395
and REF 012378 breakouts, are collectively referred to as Connex herein. Either breakout works with either base. The two base models are functionally equivalent for
purposes of this project, as are the two breakout models. Note: I have recently seen REF 10397, a Brain Monitor base unit without Masimo support and REF 10310, a
breakout from prior to the Natus acquisition of Xltek. I have not tested these models, however my expectation is that they will work with this bridge.

It sits between NeuroWorks and the Connex base, forwards the vendor protocol unchanged, and passively decodes the live acquisition stream for publication over LSL.

Current release: **0.1b (beta)**

> This project is intended for research, interoperability, preservation, and engineering use. It is **not intended for clinical diagnosis, patient monitoring, or patient care**.

---

## What it does

In proxy mode:

```text
NeuroWorks  <---- TCP/2200 ---->  Connex-LSL  <---- TCP/2200 ---->  Connex base
                                     |
                                     +----> LSL
```

NeuroWorks continues to control the hardware normally. Connex-LSL forwards traffic in both directions while inspecting a copy of the acquisition stream.

The proxy does not need to emulate normal Connex control functions. NeuroWorks remains responsible for:

- device initialization
- acquisition start/stop
- impedance checking
- normal calibration mode
- photic control
- keepalives
- session cleanup

The proxy has been tested with ordinary acquisition, impedance checking, normal user-accessible calibration mode, device information queries, and multiple short-lived NeuroWorks connections.

---

## Current features

- Transparent TCP/2200 pass-through
- Multiple simultaneous NeuroWorks TCP connections
- One matching outbound Connex connection per NeuroWorks connection
- Automatic teardown of both sides if either half of a connection fails
- Allows NeuroWorks to perform its normal reconnect behavior
- Passive decoding of 140-byte Connex acquisition packets
- 256 Hz and 512 Hz acquisition-rate detection
- 50-word sample-frame decoding
- D6 per-channel factory gain coefficient capture
- EEG scaling using D6 coefficients
- LSL EEG stream
- LSL auxiliary stream
- LSL marker stream
- Patient-event input exposed as:
  - a continuously sampled 0/1 auxiliary channel
  - a `PATIENT_EVENT` marker on the rising edge
- SpO2 decoding
- pulse-rate decoding
- scan-index continuity checking
- verbose protocol logging to the console

---

## Requirements

- Python 3.9 or newer
- `pylsl`
- A working Connex base/headbox
- NeuroWorks configured to connect to the proxy instead of directly to the base

Install the Python LSL library:

```bash
pip install pylsl
```

---

## Basic usage

Assuming:

- Connex base: `192.168.2.2`
- proxy machine: the same Windows PC running NeuroWorks
- NeuroWorks configured to connect to `127.0.0.1`

Run:

```bash
python connex_lsl_proxy_0.1b.py --device 192.168.2.2
```

The proxy listens by default on:

```text
127.0.0.1:2200
```

and connects to the real Connex at:

```text
192.168.2.2:2200
```

To test only the pass-through layer without LSL:

```bash
python connex_lsl_proxy_0.1b.py --device 192.168.2.2 --no-lsl
```

For additional protocol logging:

```bash
python connex_lsl_proxy_0.1b.py --device 192.168.2.2 --verbose
```

---

## NeuroWorks configuration

Auto-discovery is not expected to work through a loopback proxy.

Add the device manually by IP address and point NeuroWorks at:

```text
127.0.0.1
```

NeuroWorks may create several TCP connections while querying or operating the device. This is normal. Connex-LSL accepts each incoming connection and creates a corresponding independent connection to the real base.

---

## LSL streams

Connex-LSL currently creates three LSL streams.

### EEG

```text
Connex_EEG_<connection-id>
```

38 channels:

```text
     FP1 FPZ FP2
   F7 F3 FZ F4 F8
A1 T3 C3 CZ C4 T4 A2
   T5 P3 PZ P4 T6
        O1 O2
LOC CHIN1 ECGL LAT1 RAT1
ROC CHIN2 ECGR LAT2 RAT2
DIF1 DIF2 DIF3 DIF4 DIF5 DIF6
```

The channel ordering was verified using a signal generator. The 32 referential channels follow the physical headbox layout from left to right, top to bottom. DIF1-DIF6 were also independently verified.

For ordinary EEG channels, the current conversion uses the captured D6 factory gain coefficient and the published nominal Connex resolution:

```text
referential:   0.3 uV/count
differential:  0.6 uV/count
```

with:

```text
corrected = raw * (D6 / 32768)
```

followed by the nominal microvolt-per-count conversion.

### AUX

```text
Connex_AUX_<connection-id>
```

Current channels:

```text
DC1
DC2
DC3
DC4
DC5
DC6
DC7
DC8
DC9
DC10
OSAT
PR
PATIENT_EVENT
```

At present, the DC channels are published as **raw signed counts**. Their manual-specified quantization is 0.2 mV/count, but the conversion is intentionally left uncommitted in the beta until the breakout and base DC paths are verified against known voltages.

OSAT is published in percent.

Pulse rate is published in beats per minute.

### Markers

```text
Connex_Markers_<connection-id>
```

Current markers include:

```text
ACQUISITION_START
ACQUISITION_STOP
PATIENT_EVENT
```

---

## Connex acquisition packet layout

The currently decoded 140-byte acquisition message is:

```text
0x00-01   total message length = 140
0x02-0B   header / unknown
0x0C-0D   sequence-like value
0x0E-13   unknown
0x14-15   sample index
0x16-19   unknown
0x1A-1B   observed value 1
0x1C-1D   observed value 10
0x1E-1F   observed value 0
0x20-21   sample index
0x22      status / flags
0x23      observed value 1
0x24-25   sample-word count = 50
0x26-79   words 0-41: breakout data
0x7A-85   words 42-47: base DC inputs
0x86-87   word 48: OSAT, tenths of percent
0x88-89   word 49: pulse rate
0x8A-8B   sequence-like value
```

The first 42 sample words are copied verbatim from the breakout's high-speed serial stream into the TCP acquisition packet.

Current interpretation:

```text
0-31   referential EEG
32-37  differential inputs
38-41  breakout DC inputs
42-47  base DC inputs
48     OSAT
49     pulse rate
```

Bit 0 of byte `0x22` has been observed to correspond to the physical patient-event input.

---

## Impedance behavior

The normal NeuroWorks impedance test works through the proxy.

During impedance testing, the injected test waveform is visible in the raw acquisition stream exposed over LSL even though NeuroWorks does not normally display it. The waveform appears square-wave-like.

The impedance scanner advances one electrode at a time and NeuroWorks sends control commands as it moves between electrodes.

The six differential channels do not participate in the impedance test.

The REF electrode does participate in impedance testing even though REF is not present as an ordinary streamed EEG sample channel. This behavior is still under investigation.

---

## Connection and reconnect behavior

The proxy intentionally does not attempt to preserve a NeuroWorks-side TCP session if the real device connection dies.

If either side of a paired connection fails:

```text
close NeuroWorks side
close Connex side
discard connection state
wait for NeuroWorks to reconnect
```

This mirrors the way NeuroWorks already behaves when the real hardware goes offline.

---

## Known limitations

This is a beta reverse-engineering release.

Current limitations include:

- DC scaling is not yet experimentally verified
- photic-feedback status bit is not yet decoded
- several packet header/status fields remain unidentified
- impedance-result internals are not yet fully decoded
- LSL metadata may change as additional hardware behavior is characterized
- only the Connex protocol is implemented in this repository at present
- no attempt is made to emulate NeuroWorks or replace all of its control functions

---

## Planned work

Likely next steps include:

- verify DC quantization with known voltages
- identify photic-feedback input bit
- runtime channel substitution/flatline tool for protocol mapping
- improved file logging
- more protocol documentation
- optional direct-to-hardware mode without NeuroWorks
- additional Natus/Xltek/Nicolet hardware families

Related hardware under investigation includes:

- Nicolet v32
- Nicolet v44
- SomnoStar Z4
- Quantum / Quantum II based systems
- EMU40EX
- newer Brain Monitor systems

The long-term goal is a practical open interoperability layer for useful EEG hardware that is otherwise tied to proprietary acquisition software.

---

## Safety and intended use

This software is experimental.

It should not be relied upon for:

- diagnosis
- clinical monitoring
- treatment decisions
- emergency use
- any situation where incorrect, delayed, missing, or mis-scaled data could harm a person

If you are using this project for research or engineering work, independently verify channel order, scaling, timing, and signal integrity for your own hardware.

---

## Project credits

Protocol reverse engineering, hardware validation, project design, testing, and maintenance by the project maintainer.

Implementation developed with assistance from OpenAI ChatGPT.

The project deliberately documents that AI-assisted code generation was used rather than making a stronger authorship claim than necessary.

---

## License

A permissive open-source license such as **MIT** is recommended for this project.

If this repository includes an `LICENSE` file, that file controls the actual license terms.

---

## Status

**0.1b — beta**

The proxy and LSL path are functional on tested Connex hardware, but the project should still be treated as experimental and subject to protocol and metadata changes.
