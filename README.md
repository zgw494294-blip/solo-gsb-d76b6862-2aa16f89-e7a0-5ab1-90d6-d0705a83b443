# Offline HLS Window Reconciler

离线 HLS 窗口复原 API。输入一份播放列表地址与按时间顺序排列的若干份清单快照（playlist 文本），服务在内存中解析并跨快照合并，复原出滑动窗口覆盖的完整片段时间线，返回每个片段的精确十进制起止秒数。**全程离线**：清单由请求体提供，服务不做任何网络抓取。

## 快速启动

仅需一条命令（Docker Compose）：

```bash
docker compose up --build
```

服务监听 **8000** 端口：

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：`GET http://localhost:8000/healthz`
- 复原接口：`POST http://localhost:8000/v1/reconcile`

也可以不用 Compose：

```bash
docker build -t hls-window-reconciler .
docker run --rm -p 8000:8000 hls-window-reconciler
```

### 本地开发（不用 Docker）

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 启动服务
pytest                                      # 运行测试（29 个用例）
```

## API

### `POST /v1/reconcile`

**请求体**

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `playlist_url` | string (URL) | 必填，绝对 http(s) URL | 清单地址，仅用作相对 URI 的解析基准，不会发起请求 |
| `snapshots` | string[] | 2～30 份，按时间顺序 | 每份为完整的 media playlist 文本，须含 1～100 个片段 |

**请求示例**

```bash
curl -X POST http://localhost:8000/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "playlist_url": "https://cdn.example.com/live/stream.m3u8",
    "snapshots": [
      "#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXT-X-MEDIA-SEQUENCE:100\n#EXT-X-DISCONTINUITY-SEQUENCE:2\n#EXTINF:8.0,\nseg100.ts\n#EXT-X-DISCONTINUITY\n#EXTINF:8.0,\nseg101.ts\n",
      "#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXT-X-MEDIA-SEQUENCE:101\n#EXT-X-DISCONTINUITY-SEQUENCE:2\n#EXT-X-DISCONTINUITY\n#EXTINF:8.0,\nseg101.ts\n#EXTINF:8.0,\nseg102.ts\n"
    ]
  }'
```

**响应（200）**

```json
{
  "playlist_url": "https://cdn.example.com/live/stream.m3u8",
  "snapshot_count": 2,
  "segment_count": 3,
  "start_sequence": 100,
  "end_sequence": 102,
  "total_duration": "24.0",
  "segments": [
    {
      "sequence": 100,
      "uri": "https://cdn.example.com/live/seg100.ts",
      "duration": "8.0",
      "start": "0.0",
      "end": "8.0",
      "discontinuity_sequence": 2,
      "discontinuity": false,
      "byte_range": null
    },
    {
      "sequence": 101,
      "uri": "https://cdn.example.com/live/seg101.ts",
      "duration": "8.0",
      "start": "8.0",
      "end": "16.0",
      "discontinuity_sequence": 3,
      "discontinuity": true,
      "byte_range": null
    },
    {
      "sequence": 102,
      "uri": "https://cdn.example.com/live/seg102.ts",
      "duration": "8.0",
      "start": "16.0",
      "end": "24.0",
      "discontinuity_sequence": 3,
      "discontinuity": false,
      "byte_range": null
    }
  ]
}
```

- `duration` / `start` / `end` / `total_duration` 均为**精确十进制**字符串（内部用 `Decimal` 运算，不经过浮点），窗口起点为 `0`，精度与首段时长对齐。
- `discontinuity_sequence`：片段所属的不连续计数；`discontinuity`：该片段与前一片段之间是否存在断点。
- `byte_range`：已解析的字节范围（`length` + `offset`），清单未携带时为 `null`。

**错误（422）**

所有语义错误返回 422，`detail` 中包含出错的 `snapshot_index`（从 0 起）与 `sequence`（媒体序号，如适用）：

```json
{
  "detail": {
    "error": "overlap_conflict",
    "message": "snapshot 1 conflicts with the merged window at media sequence 101: duration differs",
    "snapshot_index": 1,
    "sequence": 101,
    "field": "duration",
    "expected": "8.0",
    "actual": "8.5"
  }
}
```

| `error` | 含义 |
| --- | --- |
| `start_sequence_regression` | 后一份快照的起始序号小于前一份（序号回退） |
| `sequence_gap` | 后一份起始序号大于已合并末序号 + 1（窗口断裂） |
| `overlap_conflict` | 重叠段的 URI、十进制时长、字节范围或不连续计数不一致（附 `field`/`expected`/`actual`） |
| `invalid_playlist` | 清单解析失败（附 `line` 行号） |
| `no_segments` / `too_many_segments` | 单份快照片段数不在 1～100 |
| （请求体验证） | 快照数不在 2～30、`playlist_url` 非法等，为 FastAPI 标准 422 格式 |

## 解析与合并规则

- **EXT-X-MEDIA-SEQUENCE**：首段的媒体序号（缺省 0），清单内逐段递增；该标签须出现在首个片段之前。
- **EXTINF**：段时长，按十进制精确解析（非负有限小数）。
- **EXT-X-DISCONTINUITY-SEQUENCE**：不连续计数基数（缺省 0）。
- **EXT-X-DISCONTINUITY**：每遇到一个断点标记，不连续计数加一，自下一个片段起生效。
- **EXT-X-BYTERANGE**：`长度[@偏移]`。无偏移时，仅承接**同一份清单中紧邻的前一段**且 **URI 相同**（偏移 = 前段偏移 + 前段长度）；否则视为清单非法（422），不跨清单承接。
- **URI 解析**：片段 URI 一律相对 `playlist_url` 解析为绝对地址。
- **跨快照合并**：按媒体序号对齐。重叠段的 URI、十进制时长（数值相等即可，`8.0` 与 `8.00` 不冲突）、字节范围、不连续计数必须完全相同；起始序号回退、窗口断裂、重叠冲突均返回 422。
- 其余标签（`EXT-X-TARGETDURATION`、`EXT-X-ENDLIST` 等）与注释行被安全忽略。

## 项目结构

```
app/
  main.py         FastAPI 应用与 /v1/reconcile 端点
  parser.py       HLS 清单解析（内存中，无网络 I/O）
  reconciler.py   跨快照窗口合并与冲突检测
  models.py       请求/响应模型
tests/
  test_reconcile.py
Dockerfile
docker-compose.yml
requirements.txt      运行依赖
requirements-dev.txt  运行 + 测试依赖
```
