# FGM with Ember

本项目基于开源一维火焰求解器 [HuihuiHotpot/ember](https://github.com/HuihuiHotpot/ember) 构建 FGM（Flamelet Generated Manifold）模型数据，核心计算对象是 **1D unsteady counterflow** 非定常对向流。

当前流程已经整理为两个批量脚本：

1. `initial_mixing_line.py` 使用 `mechanism_noreactions.yaml` 计算无反应纯混合线，并生成后续瞬态火焰可用的 restart profile。
2. `igniting_from_mixing_line.py` 使用 `mechanism.yaml` 从纯混合线 profile 重启，计算非定常反应流、着火和火焰发展过程。

## 项目文件

| 文件 | 作用 |
| --- | --- |
| `initial_mixing_line.py` | 批量计算无反应纯混合线，输出 restart profile |
| `igniting_from_mixing_line.py` | 从纯混合线 profile 重启，批量计算瞬态火焰 |
| `mechanism_noreactions.yaml` | 删除反应后的机理文件，用于纯混合线计算 |
| `mechanism.yaml` | 完整反应机理文件，用于瞬态火焰计算 |

## 环境准备

先按照 Ember 仓库说明完成安装。

两个脚本都会在导入 Ember/Cantera/HDF5 相关库之前设置：

```python
HDF5_USE_FILE_LOCKING=FALSE
```

这用于减少 Windows 环境下 HDF5 文件锁导致的读写问题。

## 运行前配置

运行前主要修改两个脚本主程序区域中的配置项。不要在 README 或其他文档中固化某台机器的绝对路径；路径应只在脚本配置项中按当前设备修改。

### 纯混合线脚本

`initial_mixing_line.py` 的主要配置项：

| 配置项 | 含义 |
| --- | --- |
| `n_workers` | 并行 case 数，每个 worker 运行一个工况 |
| `n_threads` | 单个 Ember case 使用的线程数 |
| `heartbeat_interval` | 长时间运行时的控制台心跳输出间隔，单位为秒 |
| `run_root` | 纯混合线输出根目录 |
| `T_list` | 氧化剂温度列表 |
| `p_list_bar` | 压力列表，单位为 bar |
| `chist_factor` | 当前燃料对应的标量耗散率换算因子 |
| `a_list` | 应变率列表 |

当前脚本保持原有物理设置：燃料为 `C10H22/C8H18/MCH/C7H8` 混合物，氧化剂为 `N2:3.76, O2:1.0`，燃料温度 `Tfuel=373 K`，输运模型为 `UnityLewis`，纯混合线阶段使用 `mechanism_noreactions.yaml` 和 `qss` 化学积分器。

### 瞬态火焰脚本

`igniting_from_mixing_line.py` 的主要配置项：

| 配置项 | 含义 |
| --- | --- |
| `n_workers` | 并行 restart case 数 |
| `n_threads` | 单个 Ember case 使用的线程数 |
| `heartbeat_interval` | 长时间运行时的控制台心跳输出间隔，单位为秒 |
| `t_end` | 瞬态火焰计算终止时间，单位为秒 |
| `global_timestep` | 全局时间步长，单位为秒 |
| `mixing_root` | 纯混合线输出根目录，必须指向第一步的 `run_root` |
| `ignition_root` | 瞬态火焰输出根目录 |
| `case_selection` | 工况选择方式：`all` 或 `failed` |
| `case_glob` | 从 `mixing_root` 中发现工况目录时使用的 glob 模式 |

点火脚本会扫描 `mixing_root` 下符合命名规则的工况目录，并自动选取每个工况中编号最大的 `profXXXXXX.h5` 作为 restart 文件。瞬态火焰阶段使用完整机理 `mechanism.yaml`、`UnityLewis` 输运模型和 `cvode` 化学积分器。

## 运行流程

### 1. 计算无反应纯混合线

确认 `initial_mixing_line.py` 中的 `run_root`、并行参数和工况列表后运行：

```powershell
python initial_mixing_line.py
```

每个工况会写入独立子目录，目录名包含氧化剂温度、压力和应变率，例如：

```text
T=400 K_p=20 bar_a=100 s-1
```

脚本会在 `run_root` 下维护状态文件：

```text
_case_status.csv
```

典型状态包括：

| 状态 | 含义 |
| --- | --- |
| `SUCCESS` | 工况完成并通过输出检查 |
| `SKIPPED_COMPLETED` | 输出目录中已有完成记录，本次跳过 |
| `FAILED` | 工况失败，错误信息写入状态文件和 error 文件 |

### 2. 从混合线重启计算瞬态火焰

确认 `igniting_from_mixing_line.py` 中的 `mixing_root` 指向第一步输出目录，并设置 `ignition_root` 后运行：

```powershell
python igniting_from_mixing_line.py
```

脚本会自动发现可重启工况，读取每个纯混合线目录中的最终编号 profile，并在 `ignition_root` 下生成对应的瞬态火焰结果目录。瞬态火焰阶段同样会维护：

```text
_case_status.csv
```

如果只想重算上一次记录为失败的点火工况，可以把：

```python
case_selection = "failed"
```

重新设为全部工况时使用：

```python
case_selection = "all"
```

## 输出结果怎么看

第一步运行 `initial_mixing_line.py` 后，结果会写到 `run_root` 指定的位置。这个目录里会有一个 `_case_status.csv`，用来记录所有纯混合线工况的运行状态。

每个工况还会有自己的结果文件夹，文件夹名称直接写明该工况的参数，例如：

```text
T=400 K_p=20 bar_a=100 s-1
```

这个名字表示氧化剂温度是 `400 K`，压力是 `20 bar`，应变率是 `100 s-1`。文件夹里面通常会看到日志文件和 Ember 生成的 profile 文件。profile 文件名类似：

```text
prof000001.h5
prof000002.h5
prof000003.h5
```

第二步运行 `igniting_from_mixing_line.py` 时，脚本会去第一步的工况文件夹里找编号最大的 profile 文件，例如 `prof000003.h5`，然后把它作为初始场继续计算瞬态火焰。第二步结果会写到 `ignition_root` 指定的位置，也会按同样的工况名称生成文件夹，并维护自己的 `_case_status.csv`。

常见输出文件含义：

- `_case_status.csv`：记录每个工况的运行时间、状态、输出目录和错误摘要。
- `.log` 文件：Ember 求解日志，长时间运行时可用于查看当前进度。
- `profXXXXXX.h5`：Ember profile 文件，也是下一步 restart 会用到的核心结果。
- `*__error.txt`：工况失败时生成，保存 Python 异常堆栈。

大量运行结果、`.h5` 文件、日志文件和临时状态文件不应提交到 Git。

## 数据流

```text
mechanism_noreactions.yaml
        |
        v
initial_mixing_line.py
        |
        v
纯混合线 profXXXXXX.h5
        |
        v
igniting_from_mixing_line.py
        |
        v
瞬态火焰 profXXXXXX.h5
        |
        v
后续 FGM 表格构建
```

## 运行建议

1. 先用较少工况和较小 `n_workers` 测试环境、路径和 Ember 安装是否正常，再扩大批量范围。
2. `n_workers * n_threads` 不宜明显超过机器可用 CPU 线程数；如果内存压力较大，优先降低 `n_workers`。
3. 两个脚本都会跳过已经完成的工况，适合中断后继续运行。
4. 点火脚本依赖纯混合线目录命名规则和 `profXXXXXX.h5` 文件；如果手动移动或改名，需要保持目录名格式不变。
5. 若改变输出根目录，只需要同步修改脚本中的 `run_root`、`mixing_root` 和 `ignition_root`。

## 项目目标

本项目通过 Ember 的 1D unsteady counterflow 求解能力，生成覆盖不同氧化剂温度、压力和应变率条件的火焰小火焰数据库，为后续 FGM 表格构建、流场耦合和燃烧模型分析提供清晰、可追踪的基础数据。
