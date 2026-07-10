# Teleoperation Platform Marktkaart

Marktindeling en technische vergelijking van teleoperation platformen, transport-keuzes,
en een concreet testplan om Kyber en Adamo te reproduceren op onze Jetson Orin setup.

Bronnen: Adamo blog, LiveKit Portal GitHub, Transitive transAct GitHub,
RidgeRun developer wiki, IETF MoQ datatracker, websitescans (juli 2026).

---

## Marktindeling

### 1. Complete platforms

Kant-en-klare teleoperation diensten inclusief infrastructuur, monitoring en support.

| Platform | Transport | Latency clean | Latency 10% loss | Prijs | Open? |
|---|---|---|---|---|---|
| [Adamo](https://adamohq.com/) | Custom QUIC, geen jitter buffer | ~40ms | 133ms | €50/robot/mnd | Nee |
| [LiveKit Portal](https://github.com/livekit/portal) | WebRTC SFU + Rust/Python laag | ~100ms | 183ms | Usage-based | Ja (portal laag) |
| [AY-Robots](https://ay-robots.com/) | WebRTC (niet transparant) | ~100ms | ~400ms+ | €50/robot/mnd | Nee |

**Noot AY-Robots:** transportkeuze niet publiek gedocumenteerd; bij twijfel WebRTC aannemen.

---

### 2. QUIC/MoQ SDK's

Bouwblokken voor zelf implementeren — open source, geen managed infrastructure.

#### [Kyber](https://jbkempf.com/) — QUIC + RaptorQ, gebouwd door de VLC-maker

Jean-Baptiste Kempf (oprichter VideoLAN/VLC, 6 miljard downloads).
$5M seed Lightspeed, juni 2026. Hubs in Parijs, San Francisco, Singapore.

- **Transport:** QUIC + WebTransport — video, audio, sensoren én control in één socket
- **Stack:** FFmpeg (server, push-mode) + VLC (decoder, realtime-mode)
- **FEC:** RaptorQ — verloren pakketjes gereconstrueerd zonder retransmissie-round-trip
- **Latency:** 8ms glass-to-glass gedemonstreerd (Mile High Video, feb 2025); doel 4ms
- **Geen jitter buffer:** altijd de nieuwste frame, geen smoothing
- **Open source:** ja
- **Sterk punt voor ons:** FFmpeg en GStreamer delen dezelfde codec-backends (NVENC op Jetson);
  Kyber's encode-pipeline is direct porteerbaar naar Jetson Orin

#### Quicwire

Beperkte publieke informatie gevonden. Vermeld als QUIC/MoQ SDK voor teleoperation,
maar geen publieke repository of website beschikbaar op moment van schrijven (juli 2026).
*Nader onderzoeken zodra meer publiek beschikbaar is.*

#### [RidgeRun GstMoQ](https://developer.ridgerun.com/wiki/index.php/RidgeRun_Media_Over_Quic_GStreamer_Plugin_GstMoQ)

GStreamer plugin voor Media over QUIC — native Jetson integratie.

- **Transport:** MoQ over QUIC (IETF draft)
- **Stack:** GStreamer elementen (`rrmoqbin`, `rrmoqsrc`, `rrmoqsink`) — drop-in naast nvarguscamerasrc
- **Jetson:** gedocumenteerd op Jetson AGX Orin, inclusief NVENC zero-latency tuning
- **Meerdere tracks:** meerdere videotracks in één MoQ sessie (360° video demo met Meta Quest 2)
- **SEI metadata:** `GstSEI` plugin voor tijdstempels en custom metadata per frame
- **Open source:** deels (plugin is commercieel bij RidgeRun, referentie-implementatie beschikbaar)
- **Sterk punt voor ons:** onze GStreamer pipeline (`nvarguscamerasrc → nvjpegenc → tcpserversink`)
  is één-op-één te vervangen door `nvarguscamerasrc → nvh264enc → rrmoqsink`

---

### 3. QUIC/MoQ infrastructuur

Relay-netwerken en protocoldefinities — geen volledige SDK, wel bouwstenen.

#### [moq.dev](https://doc.moq.dev/) — open source MoQ reference implementatie

- Rust (native) + TypeScript (web) implementatie van IETF draft-ietf-moq-transport
- Draft versie 17 (maart 2026); RFC verwacht 2027–2028
- Control messages: SUBSCRIBE, ANNOUNCE, PUBLISH, FETCH, UNSUBSCRIBE
- Data model: tracks → groups → subgroups → objects
- GStreamer plugin beschikbaar (zie `doc.moq.dev/app/gstreamer`)
- 11 vendors demonstreerden interoperabiliteit op NAB Show 2026 (Cloudflare, AWS, Bitmovin, ...)

#### [Cloudflare MoQ](https://blog.cloudflare.com/) relay

- Cloudflare heeft een globaal MoQ relay-netwerk uitgerold
- Actieve bijdrager aan IETF MoQ specificatie
- Relevant als infrastructuurlaag voor Adamo-alternatief op internet

---

### 4. Generieke QUIC libraries

Laagste niveau — protocol implementaties zonder media-laag.

| Library | Taal | Onderhoud | Gebruik |
|---|---|---|---|
| [quiche](https://github.com/cloudflare/quiche) | Rust + C bindings | Cloudflare | Productie, ook in curl |
| [quinn](https://github.com/quinn-rs/quinn) | Pure Rust | Community | Gebruikt door moq.dev |
| [msquic](https://github.com/microsoft/msquic) | C (cross-platform) | Microsoft | Windows + Linux embedded |
| [aioquic](https://github.com/aiortc/aioquic) | Python async | Community | Prototyping, niet voor productie |

Voor onze use case: **aioquic** om snel te experimenteren op laptop-side;
**quiche** of **quinn** als we naar productie-kwaliteit willen op Jetson.

---

## Marktconclusie

> De markt bestaat op dit moment vooral uit **Adamo als product** en
> **Kyber/RidgeRun GstMoQ als opkomende technologie**.
> Er is geen duidelijk publiek, kant-en-klaar QUIC-robotteleopplatform anders dan Adamo.

Vermoedelijk ontbreekt nog: Quicwire (beperkte info), en elk platform dat
MoQ + robotica-control combineert in een managed service op Adamo-niveau.

---

## Drie concrete tests voor Teleopworks

Eerlijke vergelijking: kopen vs. open-source integreren vs. zelf de transportlaag bouwen.

### Test 1 — Adamo als managed referentie

- Doel: hardware-gemeten baseline van het beste commerciële product
- Aanpak: Adamo trial account, hun agent op de Jetson Orin, onze camera-latency-meter
  meet de echte glass-to-glass latency (niet hun marketingcijfer)
- Sessie-labels: `adamo_clean`, `adamo_5loss`, `adamo_10loss`
- Meetbaar met: `calibrate_and_overlay.py` — geen aanpassing nodig

### Test 2 — Quicwire als open MoQ experiment

- Doel: open MoQ protocol testen zodra Quicwire publiek beschikbaar is
- Aanpak: Quicwire SDK op Jetson + laptop, zelfde camera-setup
- Alternatief nu: moq.dev GStreamer plugin als tussenoplossing
- Sessie-labels: `moq_clean`, `moq_5loss`

### Test 3 — RidgeRun GstMoQ of Kyber als native Jetson/GStreamer route

Twee sub-varianten, start met RidgeRun (laagste integratiedrempel):

**3a. RidgeRun GstMoQ** (GStreamer drop-in):
```bash
# Huidige pipeline op Jetson (start_cam0_tcp.sh):
nvarguscamerasrc → nvjpegenc → tcpserversink

# Vervangen door:
nvarguscamerasrc → nvh264enc (zerolatency) → rrmoqsink
```
Laptop-side: `rrmoqsrc` → decode → brightness meting (zelfde Python logica)

**3b. Kyber** (FFmpeg-based):
```bash
# Jetson: Kyber server streamt cam0 via QUIC
kyber-server --device /dev/video0 --port 5001

# Laptop: Kyber client → frame → numpy → ROI meting
kyber-client --host 192.168.86.47 --port 5001 | python cam0_loop.py
```
Sessie-labels: `ridgerun_clean`, `kyber_clean`, `kyber_5loss`, `kyber_10loss`

### Vergelijking draaien

```bash
# Na alle sessies:
python laptop/compare_sessions.py

# Verwachte volgorde (beste → slechtste onder 10% loss):
# kyber_10loss < ridgerun_10loss < adamo_10loss < moq_10loss < tcp_10loss
```

---

## Transporthiërarchie (van laag naar hoog onder packet loss)

```
QUIC + RaptorQ FEC   (Kyber)                  8ms clean,  doel 4ms
QUIC + MoQ           (RidgeRun GstMoQ, moq.dev)  ~20-40ms geschat
Custom QUIC          (Adamo)                  ~40ms clean, 133ms @ 10% loss
Plain UDP            (onze UDP brightness mode)  snel, maar gaten bij loss
FEC over UDP         (Ottopia, proprietary)    geen gaten, geen retransmissie
WebRTC               (LiveKit, Transitive, AY-Robots)  100ms+, 183-617ms @ 10%
TCP                  (onze cam0/cam1 baseline)  100ms+, spikes bij loss
```

---

## Appendix: platformdetails

### Adamo

Sub-40ms door drie keuzes:
1. QUIC zonder WebRTC → geen head-of-line blocking
2. Geen jitter buffer → altijd nieuwste frame
3. Multi-path bonding → LTE + 5G + WiFi gecombineerd

Benchmark (Adamo blog, zelfde camera door alle drie simultaan):

| Packet loss | Adamo | LiveKit | Transitive |
|---|---|---|---|
| 0% | ~83ms | ~100ms | ~100ms |
| 10% | 133ms | 183ms | 617ms |
| 15% | ~180ms | ~220ms | stream dropped |

### LiveKit Portal

Open source Rust/Python laag bovenop WebRTC SFU.
Elke frame + elk control-pakket krijgt monotone klok-timestamp,
gebundeld in één "observation" per tick (camera + joint state + timestamp).
SCTP data channels: per stream instelbaar als reliable of unreliable.

### Transitive (open source)

`transAct` is een fork-and-customize fleet dashboard (React + ShadCn + Tailwind).
WebRTC als transport → kwetsbaar onder packet loss. Waarde: fleet management UI patronen.

### Ottopia

Proprietary FEC + multi-path bonding. AI-gebaseerde super-resolutie (−20% bandbreedte).
Focus militair + AV. Enterprise pricing, niet relevant voor robotica prototype-fase.

### Avear Robotics

Opgericht 2024, San Francisco. Eerste deal maart 2026.
Claimt 10ms latency — waarschijnlijk lokaal (USB camera, geen netwerk).
Ondersteunt 6× HD + haptic feedback. AI-integratie gepland Q4 2026.

### AY-Robots

€50/robot/mnd, WebRTC. Pay-per-hour ook beschikbaar.
Focus: AI training data collectie. Ondersteunt SO-100, Franka, ALOHA, etc.
SOC 2 compliant. Niet geschikt als latency-referentie.
