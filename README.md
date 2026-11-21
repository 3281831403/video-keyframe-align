# video-keyframe-align

## 题面

做自适应码率切片后，切点错位、关键帧间隔还忽长忽短：切点前移，重要画面被切到前一段；IDR 间隔用固定秒数，可变帧率下超间隔；短片段不合并，出现 1 帧 GOP；场景变化不吸附，切点落在场景中间；码率按片段数均分，长片段不够；IDR 过密不校验，码率暴涨；时间基换算用浮点，29.97 累计漂移；片段重叠不报错，编码器报错；批量共用 IDR 序列，视频之间串；gop=0 不校验，除零。keyframe/planner.py 是核心，tests/test_planner.py 15个红。要修的是：切点只向后吸附到最近的 GOP 边界、IDR 间距按整数帧计算、合并短片段、场景切点吸附最近场景帧、按时长比例分配码率、校验重叠/越界/IDR 密度、整数时间基、批量隔离并在任一视频非法时整体回滚、所有入口校验 gop。

## 运行方式

在仓库根目录执行：

```powershell
python -m pytest tests -q
```

初始状态必须恰好是 `15 failed`，且没有 collection、import 或 syntax error。

```text
15 failed in 0.xxs
```

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 数据与约束

- `keyframe/planner.py` 暴露 8 个函数；初始版本全部 `raise NotImplementedError`。
- 片段形如 `{"start": int, "end": int, "force_idr": bool}`；帧号从 0 开始，区间为左闭右开 `[start, end)`。
- 所有距离、间隔和时长按整数帧计算；涉及秒时使用整数分子/分母，避免浮点累计；判定容差最多 1 帧。
- 仅使用 Python 标准库（`bisect`、`math`）与 `pytest`；离线运行，不使用随机数。
- `tests/test_planner.py` 是验收契约，不可修改。
