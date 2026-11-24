qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=qpclone 입력 — lyra_vex_b
=

1) https://github.com/whit3rabbit/qpclone → Open in Colab
2) Runtime > Change runtime type > T4 GPU
3) Cell 1 에서 PROJECT_NAME = "lyra_vex_b", LANGUAGE = "Korean"
4) 업로드 프롬프트에서 ref.wav 업로드
5) REF_TEXT 칸에 ref_text.txt 내용을 붙여넣기
6) 나머지 셀 순서대로 실행 (~6시간)
7) Google Drive 에서 생성된 model.onnx 다운로드
8) 이 저장소로 가져와 검증:
   .venv-voicemem/bin/python integration/verify_piper_clone.py \
       --onnx ~/Downloads/model.onnx --persona lyra_vex_b

한국어 프리트레인 체크포인트가 없어 영어 체크포인트로 교차언어 파인튜닝한다. 학습 후 반드시 verify_piper_clone.py 로 CER 를 측정할 것.
