from lrcrequest import LrclibProvider

def save_lyrics_to_lrc_file(title: str, artist: str, duration_ms: int = None, output_filename: str = "output.lrc"):
    print(f"正在全网搜索歌词并准备导出 LRC: {artist} - {title} ...")
    
    # 1. 调用模块获取歌词数据
    result = LrclibProvider.fetch_best(title, artist, duration_ms)
    
    if not result["lines"]:
        print(f"[错误] 未能获取歌词，无法生成文件。原因: {result['reason']}")
        return False

    # 2. 将内部的 LyricLine 对象转换回标准的 .lrc 文本格式
    lrc_lines = []
    
    # 写入一些元数据标签（可选）
    lrc_lines.append(f"[ti:{result['matched_title']}]")
    lrc_lines.append(f"[ar:{result['matched_artist']}]")
    lrc_lines.append("[ve:MiniGlassPlayer LRC Exporter]")
    lrc_lines.append("") # 空行
    
    for line in result["lines"]:
        if line.time_ms != -1 and result["synced"]:
            # 有时间轴：转换毫秒为 [mm:ss.xx] 格式
            total_seconds = line.time_ms / 1000.0
            m = int(total_seconds // 60)
            s = total_seconds % 60
            # 格式化为 [mm:ss.xx]
            time_tag = f"[{m:02d}:{s:05.2f}]"
            lrc_lines.append(f"{time_tag}{line.text}")
        else:
            # 无时间轴或纯文本模式，直接输出文字
            lrc_lines.append(line.text)

    # 3. 写入本地文件
    lrc_content = "\n".join(lrc_lines)
    try:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(lrc_content)
        print(f"[成功] LRC 文件已保存到: {output_filename}")
        return True
    except Exception as e:
        print(f"[错误] 文件写入失败: {e}")
        return False

if __name__ == "__main__":
    # 调用示例：查询并直接生成一个名为 "jay_qingtian.lrc" 的标准歌词文件
    save_lyrics_to_lrc_file(
        title="悔过书 (feat. 林夕)", 
        artist="黄明志 / 林夕", 
        duration_ms=269000, 
        output_filename="huiguo.lrc"
    )