"""Generate the README's illustrated GIF. Requires Pillow; no live Herdr access."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'spring-load-demo.gif'
W, H = 800, 460

def font(size, mono=False):
    candidates = ([Path('/System/Library/Fonts/SFNSMono.ttf')] if mono else [Path('/System/Library/Fonts/SFNS.ttf')]) + [Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()

F, SMALL, MONO = font(18), font(14), font(16, True)
BG, PANEL, INK, MUTED, ACCENT = '#101419', '#1a2129', '#edf2f6', '#9baab8', '#71d3ac'

def scene(d, box):
    x,y,r,b = box
    d.rounded_rectangle(box, radius=8, fill='#254a5b')
    w,h=r-x,b-y
    d.ellipse((x+w*.73,y+h*.13,x+w*.87,y+h*.37),fill='#f2d99e')
    d.polygon([(x,y+h*.65),(x+w*.25,y+h*.24),(x+w*.56,b),(x,b)],fill='#629a97')
    d.polygon([(x+w*.2,b),(x+w*.58,y+h*.4),(r,b)],fill='#a0c6ad')
    d.rectangle((x,b-h*.14,r,b),fill='#406d70')

def render(t):
    im=Image.new('RGB',(W,H),BG); d=ImageDraw.Draw(im)
    d.text((28,18),'herdr-spring-load',font=F,fill=INK)
    d.text((W-145,22),'Illustrated demo',font=SMALL,fill=MUTED)
    d.rounded_rectangle((22,58,778,391),radius=12,fill=PANEL,outline='#35424f')
    d.line((22,99,778,99),fill='#35424f')
    d.text((40,72),'herdr',font=SMALL,fill=MUTED)
    d.rounded_rectangle((180,65,310,94),radius=5,fill='#303e4c')
    d.text((195,71),'1  workspace',font=SMALL,fill=INK)
    d.line((160,99,160,391),fill='#35424f')
    d.text((38,118),'Spaces',font=SMALL,fill=MUTED)
    d.text((38,151),'project',font=SMALL,fill=ACCENT)
    d.text((184,125),'~/project',font=MONO,fill=MUTED)
    d.text((184,159),'> ',font=MONO,fill=ACCENT)
    # Thumbnail starts outside the pane, then follows a drag into it.
    if t < 2.5:
        q=max(0,min(1,(t-.7)/1.5)); q=q*q*(3-2*q)
        x=64+q*348; y=259-q*61
        if t>=.7:
            d.line([(138,315),(290,266),(440,235)],fill='#415e62',width=2)
        d.rounded_rectangle((x-4,y-4,x+100,y+81),radius=7,fill='#25313c',outline=ACCENT)
        scene(d,(x,y,x+96,y+60))
        d.text((x+3,y+62),'photo.png',font=SMALL,fill=INK)
        if t>=.7:
            d.polygon([(x+85,y+59),(x+85,y+82),(x+91,y+76),(x+97,y+87),(x+102,y+84),(x+96,y+73),(x+105,y+73)],fill=INK,outline=BG)
        caption='Drag an image into a herdr pane'
    elif t < 3.1:
        d.text((209,159),'/Users/me/photo.png',font=MONO,fill=INK)
        caption='The helper confirms the dropped image'
    else:
        d.rounded_rectangle((229,111,713,370),radius=8,fill='#11181f',outline=ACCENT,width=2)
        d.text((248,122),'Image',font=SMALL,fill=INK)
        scene(d,(321,150,621,329))
        d.text((249,344),'photo.png    any key closes',font=SMALL,fill=MUTED)
        caption='A preview opens automatically'
    d.text((28,414),caption,font=F,fill=INK)
    return im

if __name__ == '__main__':
    OUT.parent.mkdir(parents=True,exist_ok=True)
    frames=[render(i/10) for i in range(62)]
    frames[0].save(OUT,save_all=True,append_images=frames[1:],duration=100,loop=0,optimize=True)
    render(4).save(OUT.with_suffix('.png'))
    print(OUT)
