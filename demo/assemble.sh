#!/bin/bash
# Assemble the narrated demo: rendered intro (with its own voice) + the screen capture with narration.
# usage: ./assemble.sh SOURCE_CAPTURE.mp4 OUT.mp4
set -euo pipefail
cd "$(dirname "$0")"
SRC=$1; OUT=$2
FF=${FF:-$HOME/.local/bin/ffmpeg}
PLAN=CAPTURE_NARRATION.txt
VO=work/cvoice
HOLD=1.5   # the last frame holds under the closing line

python3 gate.py "$PLAN" "$VO" > work/gate.txt || { cat work/gate.txt; exit 1; }

introdur=$(${FP:-$HOME/.local/bin/ffprobe} -v error -show_entries format=duration -of csv=p=0 work/intro.mp4)
capdur=$(${FP:-$HOME/.local/bin/ffprobe} -v error -show_entries format=duration -of csv=p=0 "$SRC")
total=$(python3 -c "print(round($capdur + $HOLD, 3))")

# 1. narration bed for the capture: every line placed at its start
inputs=(-f lavfi -t "$total" -i anullsrc=r=48000:cl=stereo)
chains=""; mix="[0:a]"; n=1
while IFS='|' read -r id start win text; do
  [[ $id =~ ^C[0-9] ]] || continue
  inputs+=(-i "$VO/$id.mp3")
  ms=$(python3 -c "print(int(round($start*1000)))")
  chains+="[$n:a]aresample=48000,aformat=channel_layouts=stereo,adelay=${ms}|${ms}[v$n];"
  mix+="[v$n]"; n=$((n+1))
done < "$PLAN"
$FF -hide_banner -v error -y "${inputs[@]}" -filter_complex "${chains}${mix}amix=inputs=$n:normalize=0:duration=first,highpass=f=70,acompressor=threshold=-20dB:ratio=3:attack=5:release=120,alimiter=limit=0.85,loudnorm=I=-16:TP=-1.5:LRA=11,volume=-1.5dB[out]" -map "[out]" -c:a pcm_s16le work/capture_vo.wav

# 2. intro + capture, one canvas, one encode
$FF -hide_banner -v error -y -i work/intro.mp4 -i "$SRC" -i work/capture_vo.wav -filter_complex "\
[0:v]scale=1920:1080,fps=30,setsar=1,format=yuv420p[v0];\
[1:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,fps=30,setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration=$HOLD[v1];\
[0:a]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-1.5:LRA=11,volume=-1.5dB,apad,atrim=0:$introdur[a0];\
[2:a]aresample=48000,aformat=channel_layouts=stereo[a1];\
[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]" \
  -map "[v]" -map "[a]" -c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart "$OUT"
echo "wrote $OUT"
