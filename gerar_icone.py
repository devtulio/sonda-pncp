"""Gera sonda.ico (radar). Uso: python gerar_icone.py"""
import math
from PIL import Image, ImageDraw

S = 1024  # supersampling; reduz por LANCZOS
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
c, R = S // 2, S // 2 - 24
VERDE, CLARO = (60, 220, 120), (170, 255, 200)

d.ellipse((c - R, c - R, c + R, c + R), fill=(10, 30, 22, 255), outline=VERDE, width=28)
for f in (0.66, 0.33):
    r = int(R * f)
    d.ellipse((c - r, c - r, c + r, c + r), outline=(40, 150, 85, 255), width=14)
d.line((c - R, c, c + R, c), fill=(40, 150, 85, 255), width=14)
d.line((c, c - R, c, c + R), fill=(40, 150, 85, 255), width=14)

ov = Image.new("RGBA", (S, S), (0, 0, 0, 0))
o = ImageDraw.Draw(ov)
# varredura: leque esmaecendo atrás do ponteiro (ponteiro a -50°, gira horário)
ang = -50
for i in range(60):
    a0 = ang - 70 + i * (70 / 60)
    o.pieslice((c - R + 28, c - R + 28, c + R - 28, c + R - 28), a0, a0 + 70 / 60 + 0.6,
               fill=(60, 220, 120, int(8 + i * 3.2)))
img.alpha_composite(ov)
d = ImageDraw.Draw(img)
x, y = c + (R - 28) * math.cos(math.radians(ang)), c + (R - 28) * math.sin(math.radians(ang))
d.line((c, c, x, y), fill=CLARO, width=26)
d.ellipse((c - 30, c - 30, c + 30, c + 30), fill=CLARO)
# alvo detectado
bx, by = c + int(R * 0.5 * math.cos(math.radians(-30))), c + int(R * 0.5 * math.sin(math.radians(-30)))
d.ellipse((bx - 42, by - 42, bx + 42, by + 42), fill=(255, 190, 60, 255))

img.resize((256, 256), Image.LANCZOS).save(
    "sonda.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
img.resize((256, 256), Image.LANCZOS).save("docs/img/icone-radar.png")
