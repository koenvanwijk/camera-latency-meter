# Teleoperation Platform Vergelijking

Overzicht van commerciële en open-source teleoperation platformen, hun transport-keuzes
en wat dit betekent voor latency-optimalisatie.

Bronnen: Adamo blog, LiveKit Portal GitHub, Transitive transAct GitHub, websitescans (juli 2026).

---

## Platform vergelijking

| Platform | Transport | Latency clean | Latency 10% loss | Prijs | Focus |
|---|---|---|---|---|---|
| [Adamo](https://adamohq.com/) | Custom QUIC (geen WebRTC) | ~40ms | 133ms | €50/robot/mnd | Professionele teleop |
| [LiveKit Portal](https://github.com/livekit/portal) | WebRTC SFU + Rust layer | ~100ms | 183ms | Usage-based | Data collectie + AI inference |
| [Transitive](https://github.com/transitiverobotics/transact) | WebRTC (open source) | ~100ms | 617ms → drop | Subscription | Fleet management dashboard |
| [AY-Robots](https://ay-robots.com/) | WebRTC | ~100ms | ~400ms+ | €50/robot/mnd | AI training data collectie |
| [Ottopia](https://www.ottopia.tech/) | Proprietary + FEC | "ultra-low" | onbekend | Enterprise | Militair / autonome voertuigen |
| [Avear Robotics](https://www.avearobotics.com/) | Onbekend | Claimt 10ms | onbekend | Onbekend | Nieuw (2024), haptics, 6× HD |

---

## Kernlessen per platform

### Adamo — custom QUIC, sub-40ms

Sub-40ms glass-to-glass door drie keuzes gecombineerd:

1. **QUIC in plaats van WebRTC** — geen head-of-line blocking per stream; controle-pakketjes
   hebben hogere prioriteit dan video
2. **Geen jitter buffer** — altijd de nieuwste frame, ook als er frames zijn gemist;
   vloeiendheid is bewust opgeofferd voor lagere latency
3. **Multi-path bonding** — LTE + 5G + WiFi tegelijk voor redundantie onder packet loss

Benchmark (Adamo blog, zelfde camera door alle drie tegelijk):

```
Packet loss    Adamo    LiveKit    Transitive
0%             ~83ms    ~100ms     ~100ms
10%            133ms    183ms      617ms
15%            ~180ms   ~220ms     stream dropped
```

### LiveKit Portal — WebRTC + gesynchroniseerde observaties

Open source Rust/Python laag bovenop WebRTC SFU.
Interessant: elke frame én elk control-pakket krijgt een monotone klok-timestamp,
dan worden camera + joint state + timestamp gebundeld in één "observation" per tick.
Dit is precies de meting die de camera-latency-meter ook doet — validatie van de aanpak.

SCTP data channels: per stream instelbaar als reliable of unreliable.
Voor control: unreliable (gooi oude commando's weg). Voor configuratie: reliable.

### Transitive — open source referentie

`transAct` is een fork-and-customize fleet dashboard (React + ShadCn + Tailwind).
WebRTC als transport → zelfde kwetsbaarheid als LiveKit onder packet loss.
Waarde: je kunt de broncode bestuderen voor fleet management UI patronen.

### Ottopia — FEC in plaats van retransmissie

Ottopia gebruikt Forward Error Correction in plaats van TCP-retransmissie:

```
TCP:       verlies → wacht op retransmissie → spike (50–200ms extra)
Plain UDP: verlies → frame weg → gat in signaal
FEC UDP:   verlies → receiver reconstrueert uit redundante data → geen spike, geen gat
```

Extra: AI-gebaseerde super-resolutie reduceert bandbreedte met 20% zonder kwaliteitsverlies.
Focus op militaire en AV-markt; enterprise pricing.

### Avear Robotics — 10ms claim (lokaal)

Glass-to-glass over een netwerk is fysisch niet minder dan ~RTT/2 + encode + decode.
10ms is alleen haalbaar zonder netwerk: lokale USB camera rechtstreeks op de laptop.
Dit is precies wat `--usb-cam1` (Feature 7) doet — architectureel gelijk aan Avear's aanpak.

Ondersteunt tot 6 gelijktijdige HD-streams + haptic feedback.
Eerste commerciële deal: maart 2026. Nog vroeg stadium.

### AY-Robots — data collectie focus

Zelfde prijs als Adamo (€50/robot/mnd), maar WebRTC-transport.
Primaire use case: AI training data verzamelen, niet real-time teleoperatie.
Ondersteunt SO-100, Franka, ALOHA en andere populaire robot arms.
SOC 2 compliant, end-to-end encryptie.

---

## Transport hiërarchie voor lossy netwerken

```
Laagste latency onder packet loss:
  FEC over UDP    (Ottopia)              ← geen retransmissie, geen gaten
  Custom QUIC     (Adamo)               ← geen HoL-blocking, geen jitter buffer
  Plain UDP       (onze UDP mode)       ← gooi verloren frames weg
  WebRTC          (LiveKit, Transitive, AY-Robots)  ← jitter buffer = verborgen delay
  TCP             (onze cam0/cam1 stream)  ← retransmissie = spikes bij loss
Hoogste latency onder packet loss
```

---

## Wat dit betekent voor deze meter

### Huidige architectuur vs. commerciële platformen

| Component | Onze meter | Commercieel equivalent |
|---|---|---|
| cam0 (TCP, Jetson) | Basis TCP stream | Transitive / AY-Robots niveau |
| cam1 UDP brightness | Plain UDP, geen video | Richting Adamo (geen jitter buffer) |
| cam1 USB lokaal | Geen netwerk | Avear niveau (10ms mogelijk) |

### Vergelijkbare meting zelf uitvoeren

Dezelfde benchmark als de Adamo blog, maar op eigen hardware en met hardware-bewijs:

```bash
# Sessie 1: TCP cam1 (baseline)
python laptop/calibrate_and_overlay.py --session tcp_clean

# Sessie 2: USB cam1 (geen netwerk-hop)
python laptop/calibrate_and_overlay.py --usb-cam1 0 --session usb_clean

# Sessie 3: UDP brightness mode
# Start met Feature 6 aan
python laptop/calibrate_and_overlay.py --session udp_clean

# Herhaal met packet loss via tc netem op de Jetson-interface:
sudo tc qdisc add dev eth0 root netem loss 5%
# → sessies met _5loss suffix

sudo tc qdisc change dev eth0 root netem loss 10%
# → sessies met _10loss suffix

sudo tc qdisc del dev eth0 root   # achteraf opruimen

python laptop/compare_sessions.py
```

### Volgende optimalisatie-stap

Op basis van deze vergelijking is **FEC over UDP** de grootste sprong die nog te maken is:
- Geen retransmissie-spikes zoals TCP
- Geen gaten in het signaal zoals plain UDP
- Python libraries: `zfec`, `raptorq` (Reed-Solomon / Raptor codes)
- Implementatie: zender stuurt N + K pakketjes, ontvanger reconstrueert frame uit
  willekeurige N van de N + K ontvangen pakketjes

Dit is de aanpak die Ottopia industrieel toepast en die in latency tussen QUIC en plain UDP zit.
