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
    switched=t>=2.2
    space=t>=4.6
    focused=t>=6.4
    released=t>=7.6
    tab='files' if switched else 'terminal'
    for x,label in [(180,'terminal'),(325,'files')]:
        d.rounded_rectangle((x,65,x+130,94),radius=5,fill='#365249' if label==tab else '#25303b')
        d.text((x+15,71),label,font=SMALL,fill=INK if label==tab else MUTED)
    d.line((22,99,778,99),fill='#35424f')
    d.line((160,99,160,391),fill='#35424f')
    d.text((38,117),'Spaces',font=SMALL,fill=MUTED)
    for y,label,selected in [(152,'project',not space),(199,'assets',space)]:
        d.rounded_rectangle((30,y-8,151,y+27),radius=5,fill='#365249' if selected else PANEL)
        d.text((40,y),label,font=SMALL,fill=INK if selected else MUTED)
    d.text((183,120),'~/assets' if space else '~/project',font=MONO,fill=MUTED)
    d.line((477,148,477,391),fill='#35424f')
    d.text((184,160),'> ',font=MONO,fill=ACCENT)
    d.text((499,160),'> ',font=MONO,fill=ACCENT)
    if focused:
        d.rectangle((479,149,776,389),outline=ACCENT,width=2)
    if released:
        d.text((520,160),'photo.png',font=MONO,fill=INK)
    else:
        def lerp(a,b,u):
            u=max(0,min(1,u));u=u*u*(3-2*u)
            return a+(b-a)*u
        if t<.9: x,y=lerp(80,375,t/.9),lerp(310,81,t/.9)
        elif t<2.6: x,y=375,81
        elif t<3.5: x,y=lerp(375,90,(t-2.6)/.9),lerp(81,212,(t-2.6)/.9)
        elif t<5: x,y=90,212
        elif t<5.8: x,y=lerp(90,620,(t-5)/.8),lerp(212,243,(t-5)/.8)
        else: x,y=620,243
        d.polygon([(x,y),(x,y+23),(x+6,y+17),(x+12,y+28),(x+17,y+25),(x+11,y+14),(x+20,y+14)],fill=INK,outline=BG)
        d.rounded_rectangle((x+22,y+18,x+131,y+48),radius=5,fill='#344b50',outline=ACCENT)
        d.text((x+30,y+23),'photo.png',font=SMALL,fill=INK)
        # Dwell progress rings mark the target before it opens.
        if .9<=t<2.2 or 3.5<=t<4.6 or 5.8<=t<6.4:
            start,end=(.9,2.2) if t<2.2 else ((3.5,4.6) if t<4.6 else (5.8,6.4))
            d.arc((x-13,y-13,x+17,y+17),-90,-90+360*(t-start)/(end-start),fill=ACCENT,width=3)
    caption=('Keep holding the file while you drag' if t<.9 else
             'Hover over a tab to switch to it' if t<2.6 else
             'Hover over a space to open that workspace' if t<5 else
             'Hover over the destination pane to focus it' if t<7.6 else
             'Release to drop the file in the destination pane')
    d.text((28,414),caption,font=F,fill=INK)
    return im

if __name__ == '__main__':
    OUT.parent.mkdir(parents=True,exist_ok=True)
    frames=[render(i/10) for i in range(92)]
    frames[0].save(OUT,save_all=True,append_images=frames[1:],duration=100,loop=0,optimize=True)
    render(8).save(OUT.with_suffix('.png'))
    print(OUT)
