# 离线 HLS 窗口复原 API

按时间顺序抓取的多份 HLS 媒体清单（snapshots）只是同一个直播切片窗口的不同视图。
本服务把 **2～30 份**清单（每份 **1～100 段**）按媒体序号合并去重，复原出一条
连续、带精确小数起止秒数的切片时间线。**完全离线**：服务不发起任何网络请求，
`playlist_url` 仅用于解析相对 URI。

## 单条命令启动

```bash
docker compose up --build
```

服务监听 **8000** 端口（容器内 `8000`，映射到宿主机 `8000`）：

- API：`POST http://localhost:8000/v1/reconcile`
- 健康检查：`GET http://localhost:8000/healthz`
- 交互式文档：`http://localhost:8000/docs`

> 改用其他宿主端口：`HOST_PORT=9000 docker compose up --build`（需在
> `docker-compose.yml` 中把端口写成 `"${HOST_PORT:-8000}:8000"`，或直接编辑该文件）。

不用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求

`POST /v1/reconcile`

```json
{
  "playlist_url": "https://cdn.example.com/live/index.m3u8",
  "snapshots": ["#EXTM3U\n...第 0 份清单文本...", "#EXTM3U\n...第 1 份..."]
}
```

| 字段 | 说明 |
| --- | --- |
| `playlist_url` | 清单的 URL，仅作为相对 URI 的解析基址，不会被请求 |
| `snapshots` | **按抓取时间顺序**排列的清单原文数组；长度 2～30 |

## 响应（200）

```json
{
  "playlist_url": "https://cdn.example.com/live/index.m3u8",
  "count": 3,
  "duration": "6.833",
  "segments": [
    {
      "sequence": 100,
      "uri": "https://cdn.example.com/live/big.ts",
      "duration": "2.000",
      "discontinuity": 3,
      "byte_offset": 0,
      "byte_length": 100,
      "start": "0",
      "end": "2.000"
    },
    {
      "sequence": 101,
      "uri": "https://cdn.example.com/live/big.ts",
      "duration": "1.500",
      "discontinuity": 4,
      "byte_offset": 100,
      "byte_length": 50,
      "start": "2.000",
      "end": "3.500"
    }
  ]
}
```

- `duration` / `start` / `end` 为**精确十进制**字符串（内部用 `Decimal` 计算，
  不经过二进制浮点），`start`/`end` 为秒级时间轴上的半开区间 `[start, end)`。
- 没有字节范围的段，`byte_offset` 与 `byte_length` 为 `null`。
- `discontinuity` 为复原后的不连续计数（见下）。

## 解析与合并规则

支持的标签：`#EXTM3U`、`#EXT-X-MEDIA-SEQUENCE`、`#EXTINF`、
`#EXT-X-DISCONTINUITY-SEQUENCE`、`#EXT-X-DISCONTINUITY`、
`#EXT-X-BYTERANGE`；其余标签一律忽略。

1. **序号**：段序号从 `EXT-X-MEDIA-SEQUENCE`（缺省为 `0`）起逐段加一。
2. **URI**：段 URI 按 RFC 3986 相对 `playlist_url` 解析（`urljoin`）。
3. **时长**：`EXTINF` 时长按十进制数解析并原样保留精度（如 `1.10`）。
4. **不连续计数**：以 `EXT-X-DISCONTINUITY-SEQUENCE`（缺省为 `0`）为基数，
   每遇到一个 `#EXT-X-DISCONTINUITY` 标记计数加一，随后一段携带新计数值。
5. **字节范围**：`EXT-X-BYTERANGE:n@o` 取显式偏移 `o`；仅写作 `n` 而无偏移时，
   只允许承接**同一份清单内紧邻的前一段**，且该段必须满足：
   - URI 与当前段完全相同，并且
   - 具有已确定的字节范围；

   此时偏移 = 前段偏移 + 前段长度。否则该清单视为非法。
6. **跨快照合并**：以序号为主键合并。后一份快照：
   - 起始序号 **小于前一份快照的起始序号**（序号回退）→ 422；
   - 与已合并内容不重叠但起始序号 **大于已合并末序号 + 1**（出现空洞）→ 422；
   - 与旧段重叠时，**URI、十进制时长、解析后的字节范围、不连续计数** 四者必须
     完全一致，任一不同 → 422。
7. 合并后从最早序号到最晚序号必须连续无缺，再从 0 秒起逐段累加时长，输出每段的
   精确 `start`/`end`。

## 错误响应（422）

所有复原类错误均为 HTTP `422`，并在响应体中给出**快照下标（从 0 起）与序号**：

```json
{
  "error": "overlap_conflict",
  "detail": "snapshot 1 conflicts with an earlier snapshot at sequence 101",
  "snapshot_index": 1,
  "sequence": 101
}
```

| `error` | 触发条件 |
| --- | --- |
| `invalid_request` | 快照数量不在 2～30、字段缺失/类型错误等（无具体快照时无定位字段） |
| `invalid_playlist` | 清单无法解析（缺 `#EXTM3U`、`EXTINF` 非法、字节范围承接失败等），带 `snapshot_index` |
| `empty_playlist` | 快照内没有任何段，带 `snapshot_index` |
| `too_many_segments` | 单份快照超过 100 段，带 `snapshot_index` |
| `sequence_rollback` | 起始序号回退，带 `snapshot_index` 与 `sequence` |
| `sequence_gap` | 后份快照起点超过已合并末序号 + 1，带 `snapshot_index` 与 `sequence` |
| `overlap_conflict` | 重叠序号的 URI/时长/字节范围/不连续计数不一致，带 `snapshot_index` 与 `sequence` |

## 快速验证

```bash
curl -s http://localhost:8000/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "playlist_url": "https://cdn.example.com/live/index.m3u8",
    "snapshots": [
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:100\n#EXT-X-DISCONTINUITY-SEQUENCE:3\n#EXTINF:2.000,\n#EXT-X-BYTERANGE:100@0\nbig.ts\n#EXT-X-DISCONTINUITY\n#EXTINF:1.500,\n#EXT-X-BYTERANGE:50\nbig.ts\n",
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:101\n#EXT-X-DISCONTINUITY-SEQUENCE:3\n#EXT-X-DISCONTINUITY\n#EXTINF:1.500,\n#EXT-X-BYTERANGE:50@100\nbig.ts\n#EXTINF:3.333,\nclip.ts\n"
    ]
  }'
```

## 项目结构

```
app/
  hls.py         # 离线清单解析（序号 / 时长 / 断点 / 字节范围 / URI）
  reconcile.py   # 跨快照合并、连续性与一致性校验、时间轴计算
  main.py        # FastAPI 路由、请求/响应模型、422 错误处理
tests/test_api.py
Dockerfile
docker-compose.yml
requirements.txt
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```
