//
// NT35510 LCD Controller - Renode Peripheral Model
// FSMC 16-bit interface, NE4 @ 0x6C000000, A6(PF12) as RS
//
// Struct mapping: base=0x6C00007E, CMD@0 (RS=0), DAT@2 (RS=1)
// RS determined by byte offset bit 7 (FSMC 16-bit mode A6 mapping)
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Video
{
    public class NT35510 : IDoubleWordPeripheral, IWordPeripheral, IKnownSize
    {
        private const int WIDTH = 800;
        private const int HEIGHT = 480;
        private const ushort CMD_RAMWR = 0x2C;
        private const ushort CMD_RAMRD = 0x2E;
        private const ushort CMD_CASET = 0x2A;
        private const ushort CMD_RASET = 0x2B;

        private ushort currentCmd;
        private ushort[,] fb;
        private int wx1, wy1, wx2, wy2;
        private int cx, cy;
        private int casetCount, rasetCount;
        private Dictionary<ushort, ushort> regs;

        public NT35510(Machine machine)
        {
            fb = new ushort[HEIGHT, WIDTH];
            regs = new Dictionary<ushort, ushort>();
            wx1 = 0; wy1 = 0; wx2 = WIDTH - 1; wy2 = HEIGHT - 1;
            cx = 0; cy = 0;
        }

        public long Size { get { return 0x100; } }

        public void Reset()
        {
            currentCmd = 0;
            Array.Clear(fb, 0, fb.Length);
            wx1 = 0; wy1 = 0; wx2 = WIDTH - 1; wy2 = HEIGHT - 1;
            cx = 0; cy = 0;
            regs.Clear();
        }

        // Helper: determine RS from address offset
        // RS = A6 = PF12. In FSMC 16-bit mode, byte addr bit 7 = A6
        private bool IsDataAccess(long offset) { return ((offset >> 7) & 1) != 0; }

        // Handle 16-bit write (called from both IWordPeripheral.WriteWord and IDoubleWordPeripheral)
        public int TotalWrites;

        private void WriteU16(long offset, ushort val)
        {
            TotalWrites++;
            if (!IsDataAccess(offset))
                HandleCmd(val);
            else
                HandleData(val);
        }

        // ─── IWordPeripheral (16-bit) ───────────────────────────
        public ushort ReadWord(long offset)
        {
            if (IsDataAccess(offset))
                return (ushort)ReadPixel();
            if (regs.ContainsKey(currentCmd))
                return regs[currentCmd];
            return 0;
        }

        public void WriteWord(long offset, ushort value)
        {
            WriteU16(offset, value);
        }

        // ─── IDoubleWordPeripheral (32-bit) ─────────────────────
        public uint ReadDoubleWord(long offset)
        {
            // Handle as two 16-bit values (FSMC 16-bit mode)
            if (IsDataAccess(offset))
                return ReadPixel();
            if (regs.ContainsKey(currentCmd))
                return regs[currentCmd];
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            // FSMC 16-bit mode: only lower 16 bits are valid
            WriteU16(offset, (ushort)(value & 0xFFFF));
        }

        // ─── NT35510 protocol handling ───────────────────────────
        private void HandleCmd(ushort cmd)
        {
            this.Log(LogLevel.Debug, "LCD CMD: 0x{0:X04X}", cmd);
            currentCmd = cmd;
            casetCount = 0;
            rasetCount = 0;
        }

        private void HandleData(ushort data)
        {
            switch (currentCmd)
            {
                case CMD_CASET:
                    casetCount++;
                    if (casetCount == 1)
                        wx1 = (data >> 8) | ((data & 0xFF) << 8);
                    else if (casetCount == 2) {
                        wx2 = (data >> 8) | ((data & 0xFF) << 8);
                        cx = wx1; cy = wy1;
                    }
                    break;

                case CMD_RASET:
                    rasetCount++;
                    if (rasetCount == 1)
                        wy1 = (data >> 8) | ((data & 0xFF) << 8);
                    else if (rasetCount == 2) {
                        wy2 = (data >> 8) | ((data & 0xFF) << 8);
                        cx = wx1; cy = wy1;
                    }
                    break;

                case CMD_RAMWR:
                    WritePixel(data);
                    break;

                default:
                    regs[currentCmd] = data;
                    break;
            }
        }

        private void WritePixel(ushort color)
        {
            if (cx >= 0 && cx < WIDTH && cy >= 0 && cy < HEIGHT)
                fb[cy, cx] = color;
            cx++;
            if (cx > wx2) { cx = wx1; cy++; }
        }

        private uint ReadPixel()
        {
            uint val = 0;
            if (cx >= 0 && cx < WIDTH && cy >= 0 && cy < HEIGHT)
                val = fb[cy, cx];
            cx++;
            if (cx > wx2) { cx = wx1; cy++; }
            return val;
        }

        // ─── Public test API ─────────────────────────────────────
        public ushort GetPixel(int x, int y)
        {
            if (x >= 0 && x < WIDTH && y >= 0 && y < HEIGHT)
                return fb[y, x];
            return 0;
        }

        public int NonZeroPixelCount()
        {
            int cnt = 0;
            for (int y = 0; y < HEIGHT; y++)
                for (int x = 0; x < WIDTH; x++)
                    if (fb[y, x] != 0) cnt++;
            return cnt;
        }

        public void FillTestPattern()
        {
            for (int y = 0; y < HEIGHT; y++)
                for (int x = 0; x < WIDTH; x++)
                    if      (x < WIDTH / 5) fb[y, x] = 0xF800;
                    else if (x < 2*WIDTH/5) fb[y, x] = 0x07E0;
                    else if (x < 3*WIDTH/5) fb[y, x] = 0x001F;
                    else if (x < 4*WIDTH/5) fb[y, x] = 0xFFE0;
                    else                    fb[y, x] = 0xFFFF;
        }
    }
}
