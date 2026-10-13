#include <stdint.h>

volatile uint32_t tick;
static uint8_t big_buf[1200];

int mpu6050_read(uint8_t reg);
int shtp_parse(const uint8_t *p, int n);
void imu_task(void);

static int helper_add(int a, int b) { return a + b + (int)tick; }

int mpu6050_read(uint8_t reg)
{
    volatile uint32_t *p = (volatile uint32_t *)0x40021000u;
    return (int)(*p) + reg;
}

int shtp_parse(const uint8_t *p, int n)
{
    int s = 0;
    int i;
    for (i = 0; i < n; i++) s += p[i];
    return helper_add(s, n);
}

void imu_task(void)
{
    int v = mpu6050_read(0x2A);
    if (v > 100000) big_buf[0] = (uint8_t)v;
    (void)shtp_parse(big_buf, 8);
}

int main(void)
{
    imu_task();
    return 0;
}
