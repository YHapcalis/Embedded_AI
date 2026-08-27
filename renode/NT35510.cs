//
// NT35510 LCD 控制器 Renode 外设模型
//
// 通过 FSMC 16-bit 并行接口连接：
//   CS:  FSMC_NE4 @ 0x6C000000
//   RS:  A6 (命令/数据选择)
//   写命令: *(volatile uint16_t*)(0x6C00007E) = cmd
//   写数据: *(volatile uint16_t*)(0x6C000080) = data
//
// 支持功能:
//   - 寄存器读写
//   - 帧缓冲 (240x320 RGB565)
//   - 窗口裁剪 (CASET/RASET)
//   - 帧缓冲导出，用于撕裂检测
//

using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Video
{
    public class NT35510 : IDoubleWordPeripheral, IKnownSize
    {
        // ─── 寄存器映射 ────────────────────────────────────────────
        private const ushort CMD_RAMWR   = 0x2C;  // 写显存
        private const ushort CMD_RAMRD   = 0x2E;  // 读显存
        private const ushort CMD_CASET   = 0x2A;  // 列地址范围
        private const ushort CMD_RASET   = 0x2B;  // 行地址范围

        // ─── 显示参数 ──────────────────────────────────────────────
        private const int WIDTH  = 240;
        private const int HEIGHT = 320;
        private const int PIXEL_COUNT = WIDTH * HEIGHT;

        // ─── 状态 ──────────────────────────────────────────────────
        private ushort currentCommand;       // 当前命令寄存器
        private ushort[,] framebuffer;        // [y,x] RGB565
        private int rsState;                  // 0=命令, 1=数据

        // 窗口裁剪
        private int windowX1, windowY1;
        private int windowX2, windowY2;
        private int cursorX, cursorY;

        // 注册的表
        private readonly Dictionary<ushort, ushort> registers = new Dictionary<ushort, ushort>();

        public NT35510(Machine machine)
        {
            framebuffer = new ushort[HEIGHT, WIDTH];
            // 默认全屏窗口
            windowX1 = 0; windowY1 = 0;
            windowX2 = WIDTH - 1; windowY2 = HEIGHT - 1;
            cursorX = 0; cursorY = 0;
        }

        public long Size { get { return 0x100; } }

        public void Reset()
        {
            currentCommand = 0;
            Array.Clear(framebuffer, 0, framebuffer.Length);
            windowX1 = 0; windowY1 = 0;
            windowX2 = WIDTH - 1; windowY2 = HEIGHT - 1;
            cursorX = 0; cursorY = 0;
            registers.Clear();
        }

        // ─── FSMC 16-bit 写入 ─────────────────────────────────────
        // 地址偏移量决定了 RS 状态:
        //   offset & 2 == 0 → RS=0 (命令)
        //   offset & 2 != 0 → RS=1 (数据)
        public void WriteDoubleWord(long offset, uint value)
        {
            ushort val = (ushort)(value & 0xFFFF);

            // 根据地址偏移判断 RS (A6 映射到偏移 bit 1)
            if ((offset & 2) == 0)
            {
                // RS=0: 写入命令寄存器
                HandleCommand(val);
            }
            else
            {
                // RS=1: 数据写入
                HandleData(val);
            }
        }

        // ─── FSMC 16-bit 读取 ─────────────────────────────────────
        public uint ReadDoubleWord(long offset)
        {
            if ((offset & 2) != 0)
            {
                // RS=1: 读取数据 (从显存)
                return ReadPixelData();
            }
            else
            {
                // RS=0: 读取寄存器值
                if (registers.ContainsKey(currentCommand))
                    return registers[currentCommand];
                return 0;
            }
        }

        // ─── 命令处理 ──────────────────────────────────────────────
        private void HandleCommand(ushort cmd)
        {
            currentCommand = cmd;
            this.Log(LogLevel.Debug, "LCD CMD: 0x{0:X04X}", cmd);
        }

        // ─── 数据处理 ──────────────────────────────────────────────
        private void HandleData(ushort data)
        {
            switch (currentCommand)
            {
                case 0x0000:  // NOP 或命令参数低字节
                case 0x0001:  // 命令参数高字节
                    // 部分 NT35510 命令用 2 字节参数
                    break;

                case CMD_CASET:  // 列地址范围 (2 个 16-bit 参数)
                    // 第1个参数: 起始列
                    // 第2个参数: 结束列
                    HandleColumnAddress(data);
                    break;

                case CMD_RASET:  // 行地址范围 (2 个 16-bit 参数)
                    HandleRowAddress(data);
                    break;

                case CMD_RAMWR:  // 写入显存
                    WritePixel(data);
                    break;

                case CMD_RAMRD:  // 读取显存 (当前 cursor 位置)
                    // 不执行操作，等到读取再返回
                    break;

                default:
                    // 普通寄存器写入
                    registers[currentCommand] = data;
                    break;
            }
        }

        // ─── 窗口设置 ──────────────────────────────────────────────
        private int casetParamCount = 0;
        private int rasetParamCount = 0;

        private void HandleColumnAddress(ushort data)
        {
            casetParamCount++;
            if (casetParamCount == 1) windowX1 = (data >> 8) | ((data & 0xFF) << 8);
            else if (casetParamCount == 2)
            {
                windowX2 = (data >> 8) | ((data & 0xFF) << 8);
                casetParamCount = 0;
                this.Log(LogLevel.Debug, "LCD CASET: {0}-{1}", windowX1, windowX2);
                cursorX = windowX1;
                cursorY = windowY1;
            }
        }

        private void HandleRowAddress(ushort data)
        {
            rasetParamCount++;
            if (rasetParamCount == 1) windowY1 = (data >> 8) | ((data & 0xFF) << 8);
            else if (rasetParamCount == 2)
            {
                windowY2 = (data >> 8) | ((data & 0xFF) << 8);
                rasetParamCount = 0;
                this.Log(LogLevel.Debug, "LCD RASET: {0}-{1}", windowY1, windowY2);
                cursorX = windowX1;
                cursorY = windowY1;
            }
        }

        // ─── 像素写入 ──────────────────────────────────────────────
        private void WritePixel(ushort color)
        {
            if (cursorY >= 0 && cursorY < HEIGHT && cursorX >= 0 && cursorX < WIDTH)
            {
                framebuffer[cursorY, cursorX] = color;
            }

            cursorX++;
            if (cursorX > windowX2)
            {
                cursorX = windowX1;
                cursorY++;
                if (cursorY > windowY2)
                {
                    cursorY = windowY1;
                }
            }
        }

        // ─── 像素读取 ──────────────────────────────────────────────
        private uint ReadPixelData()
        {
            if (cursorY >= 0 && cursorY < HEIGHT && cursorX >= 0 && cursorX < WIDTH)
            {
                return framebuffer[cursorY, cursorX];
            }
            return 0;
        }

        // ─── 帧缓冲访问 (Python/Robot 测试用) ─────────────────────
        public ushort GetPixel(int x, int y)
        {
            if (x >= 0 && x < WIDTH && y >= 0 && y < HEIGHT)
                return framebuffer[y, x];
            return 0;
        }

        public ushort[,] GetFramebuffer()
        {
            return framebuffer;
        }

        public void FillTestPattern()
        {
            // 彩色色条: 红/绿/蓝/黄/白
            for (int y = 0; y < HEIGHT; y++)
            {
                for (int x = 0; x < WIDTH; x++)
                {
                    if (x < WIDTH / 5)       framebuffer[y, x] = 0xF800;  // 红
                    else if (x < 2 * WIDTH / 5) framebuffer[y, x] = 0x07E0;  // 绿
                    else if (x < 3 * WIDTH / 5) framebuffer[y, x] = 0x001F;  // 蓝
                    else if (x < 4 * WIDTH / 5) framebuffer[y, x] = 0xFFE0;  // 黄
                    else                           framebuffer[y, x] = 0xFFFF;  // 白
                }
            }
        }

        public bool DetectTearing()
        {
            // 简单撕裂检测: 检查是否同一帧内有多个不连续的图像区域
            // 暂未实现复杂算法，框架预留
            return false;
        }
    }
}
