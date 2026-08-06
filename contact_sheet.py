import sys, subprocess, os
from PIL import Image
vid = sys.argv[1]; times=[float(t) for t in sys.argv[2:]]
os.makedirs('shots', exist_ok=True); ims=[]
for i,t in enumerate(times):
    f=f'shots/f{i:02d}.png'
    subprocess.run(['ffmpeg','-loglevel','error','-y','-ss',str(t),'-i',vid,'-frames:v','1',f],check=True)
    ims.append(Image.open(f))
w,h=ims[0].size; th=430; tw=int(w*th/h)
cols=len(ims); sheet=Image.new('RGB',(tw*cols,th),(20,20,20))
for i,im in enumerate(ims): sheet.paste(im.resize((tw,th)),(i*tw,0))
sheet.save('shots/sheet.png'); print(sheet.size)
