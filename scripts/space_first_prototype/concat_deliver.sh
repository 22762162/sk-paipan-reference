#!/bin/zsh
# v7 拼接与交付(mini 720p 测试版)。全部闸门:逐段尺寸+真实帧数,成片时长。
set -e
OUT=/Users/sk/AIFOS/workspace/artifacts/p014/e001/videos_v7
EP=/Users/sk/AIFOS/workspace/artifacts/p014/e001
FF=$HOME/.local/bin/ffmpeg
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs/新样片"
NAME="长夏记事_ep1_v7_全景空间版_720p测试.mp4"

n=$(ls "$OUT"/shot_*.mp4 2>/dev/null | wc -l | tr -d ' ')
echo "可用分段: $n/8"
if [ "$n" -ne 8 ]; then
  for i in 1 2 3 4 5 6 7 8; do
    [ -f "$OUT/shot_00$i.mp4" ] || echo "  缺镜$i"
  done
  exit 1
fi

for f in "$OUT"/shot_*.mp4; do
  dim=$("$FF" -i "$f" 2>&1 | grep -oE '[0-9]{3,4}x[0-9]{3,4}' | head -1 || true)
  [ "$dim" = "720x1280" ] || { echo "尺寸不一致: $f = $dim"; exit 1; }
  fr=$("$FF" -i "$f" -map 0:v:0 -f null - 2>&1 | grep -oE 'frame= *[0-9]+' | tail -1 | grep -oE '[0-9]+' || true)
  [ "$fr" -ge 115 ] 2>/dev/null || { echo "帧数不足(截断): $f = $fr"; exit 1; }
done
echo "八段校验通过: 720x1280, 帧数完整"

# concat demuxer 会静默丢帧(v6 教训),一律用 concat 滤镜重定时
inputs=(); maps=""; i=0
for f in "$OUT"/shot_*.mp4; do
  inputs+=(-i "$f"); maps="${maps}[${i}:v:0][${i}:a:0]"; i=$((i+1))
done
"$FF" -y "${inputs[@]}" \
  -filter_complex "${maps}concat=n=${i}:v=1:a=1[v][a]" \
  -map "[v]" -map "[a]" \
  -c:v libx264 -crf 17 -preset medium -pix_fmt yuv420p -r 24 \
  -c:a aac -b:a 160k -movflags +faststart \
  "$EP/$NAME" 2>&1 | tail -2

info=$("$FF" -i "$EP/$NAME" 2>&1 || true)
dur=$(echo "$info" | grep -oE 'Duration: [0-9:.]+' | head -1 | cut -d' ' -f2)
secs=$(echo "$dur" | awk -F: '{print $1*3600+$2*60+$3}')
echo "成片: $dur  $(du -h "$EP/$NAME" | cut -f1)"
awk -v s="$secs" 'BEGIN{exit !(s>37 && s<43)}' || { echo "时长异常,不交付"; exit 1; }

mkdir -p "$ICLOUD"
cp "$EP/$NAME" "$ICLOUD/"
echo "已交付 iCloud/新样片/$NAME"
