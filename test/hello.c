/* csky-elfabiv2 工具链冒烟测试：整数 + 硬件浮点 + memcpy（newlib） */
#include <stdio.h>
#include <string.h>

volatile float  fa = 1.5f, fb = 2.25f;
volatile double da = 3.75;
char buf[32];

int main(void)
{
    float  f = fa * fb + 0.125f;   /* 应生成硬件浮点指令（ck804f FPU） */
    double d = da * 2.0;
    int    i = (int)f + (int)d;

    memset(buf, 0, sizeof(buf));   /* newlib 目标库链接测试 */
    snprintf(buf, sizeof(buf), "ck804 %f", f);

    return i;                      /* 返回值可用调试器/仿真器观察 */
}
