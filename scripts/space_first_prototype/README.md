# 空间前置生产原型（v7 实证版）

2026-07-29 用《长夏记事》EP1 v7 跑通的「空间图前置」生产链原型，
成本 450 积分（对照 v6 同等迭代 1750），成片连贯性由结构保证。

## 流程

```
1. 720°全景母版+四向切片   app.director.expand_scene_views(项目, 场景)   零成本(codex)
2. 3D空间模型              以场景地标定原点/机位(勿直接搬 blocking 旧数据)
3. 逐镜背景切片            ffmpeg v360: 全景→(yaw,pitch,h_fov)透视投影      零成本
4. 九态串行链式生成        gen_states_chain.py   态N锚态N-1+背景切片        零成本(codex)
5. 闸门I多agent审查        相邻逐对比对+逐态合同核验,block清零才放行       零成本
6. mini 720p 出片          build_videos_mini.py  45积分/段,数真实帧数
7. 拼接交付                concat_deliver.sh     concat滤镜+时长闸
8. 用户确认后              同九态改 vip 1080p(165/段,需 video_final_confirmed)
```

## 铁律（每条都是实付学费换来的）

- 帧链关键帧**串行**生成:并行=每张自己发明一套房间(v6, 6/8镜换布景跳变)
- 场景事实源=全景母版,不是单角度概念图;返修必挂前后帧
- 道具尺度写**画面内参照物**(两指可捏),写厘米无效
- 空间重排后重推「谁能看见谁」:X未察觉Y ⟹ Y不在X视野内(v7 人影逻辑漏洞)
- 轮询用 query_result,勿信 list_task(状态滞后40分钟)
- 视频下载后数真实帧数,勿信容器 Duration(截断不报错)
- 拼接用 concat 滤镜,-f concat 会静默丢帧
- 迭代一律 mini 720p;vip 只给用户确认的成片(平台已 fail-closed)

配套平台闸门与修复见 docs/END_TO_START_QUALITY_CHAIN.md 与
分支 claude/codex-escalation-autofix 的提交序列。
