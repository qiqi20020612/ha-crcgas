# Changelog

## 2.1.0 (2026-09-21)

### 修复
- **统计重复累计**：月度统计导入对已存在月份改用 continue 跳过，
  以 recorder 数据库中的 last sum 作为初值，消除 sum 重复累计
- **statistics_meta 修正**：修复 has_mean/has_sum 为 null 的记录，
  并在启动 5 分钟后（recorder 编译完成）延时二次修正
- **bill_amount 兜底**：detail API 返回 0 时使用 revbl_amount 兜底

### 新增
- 24 个传感器的 state_class（MEASUREMENT/TOTAL），使长期统计正常生成
- 扫描周期 day/week/month 真正生效（原实现只有小时模式有效）

### 其他
- 全量文件与生产实测版对齐（api/config_flow/sensor_v1xx/前端卡片/中文文案）

## 2.0.7
- bootstrap CI + HACS validation workflow

## 2.0.6
- cold-start sum=0 fix for short-term statistic
