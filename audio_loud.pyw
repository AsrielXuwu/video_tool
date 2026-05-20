import os
import subprocess
import re
import sys
import csv
from datetime import datetime

def get_integrated_loudness(file_path):
    """调用 FFmpeg 计算单首音频的整体平均响度 (Integrated Loudness)"""
    cmd = [
        'ffmpeg',
        '-hide_banner',
        '-nostats',
        '-i', file_path,
        '-filter:a', 'ebur128',
        '-f', 'null',
        '-'
    ]
    
    try:
        # FFmpeg 的分析结果会输出到 stderr
        result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8')
        output = result.stderr
        
        # 使用正则表达式提取 Summary 中的 Integrated loudness (I)
        matches = re.findall(r'I:\s+(-?\d+\.\d+)\s+LUFS', output)
        
        if matches:
            return float(matches[-1])
        else:
            return None
    except FileNotFoundError:
        print("\n[错误] 找不到 FFmpeg，请确保它已安装并添加到系统环境变量中。")
        sys.exit(1)
    except Exception as e:
        print(f"\n[错误] 处理 {os.path.basename(file_path)} 时发生异常: {e}")
        return None

def batch_process_to_csv(folder_path):
    """批量处理文件夹中的音频，并导出为 CSV"""
    # 支持的常见音频格式
    supported_formats = ('.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg')
    
    if not os.path.isdir(folder_path):
        print("[错误] 输入的不是一个有效的文件夹路径！")
        return

    # 获取文件夹下的所有匹配音频文件
    audio_files = [f for f in os.listdir(folder_path) if f.lower().endswith(supported_formats)]
    
    if not audio_files:
        print("该文件夹下未找到支持的音频文件。")
        return

    # 自动生成带有时间戳的 CSV 文件名，避免覆盖历史数据
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_filename = f"音频响度报告_{timestamp}.csv"
    csv_path = os.path.join(folder_path, csv_filename)

    print(f"\n开始检测，共找到 {len(audio_files)} 个音频文件...")
    print(f"结果将自动保存至: {csv_path}")
    print("-" * 65)
    print(f"{'文件名':<45} | {'平均响度 (LUFS)':<15}")
    print("-" * 65)

    success_count = 0
    
    # 使用 'utf-8-sig' 编码写入，防止 Windows Excel 打开时中文乱码
    with open(csv_path, mode='w', newline='', encoding='utf-8-sig') as csv_file:
        writer = csv.writer(csv_file)
        # 写入 CSV 表头
        writer.writerow(['文件名', '文件路径', '平均响度 (LUFS)', '状态'])

        for filename in audio_files:
            file_path = os.path.join(folder_path, filename)
            loudness = get_integrated_loudness(file_path)
            
            if loudness is not None:
                # 终端实时打印
                print(f"{filename:<45} | {loudness:>8.1f} LUFS")
                # 写入 CSV 记录
                writer.writerow([filename, file_path, loudness, '成功'])
                success_count += 1
            else:
                print(f"{filename:<45} | {'解析失败':>10}")
                writer.writerow([filename, file_path, '', '解析失败'])

    print("-" * 65)
    print(f"处理完成！成功解析 {success_count}/{len(audio_files)} 个文件。")
    print(f"报告已生成: {csv_filename}")

if __name__ == "__main__":
    folder = input("请输入包含音频文件的文件夹路径: ").strip()
    # 去除可能包含的引号（支持拖拽文件夹到终端的行为）
    folder = folder.strip('\'"')
    batch_process_to_csv(folder)