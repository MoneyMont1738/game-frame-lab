"""PresentMon, FrameView, and nvidia-smi parsers on small hand-written logs."""

import gzip
import math

import pytest

from gfl.parse import (
    FRAME_COLUMNS,
    ParseError,
    load_frameview_csv,
    load_gpu_telemetry_csv,
    load_presentmon_csv,
)

PM2_HEADER = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,"
              "AllowsTearing,PresentMode,CPUStartTime,MsCPUBusy,MsCPUWait,MsGPULatency,MsGPUTime,"
              "MsGPUBusy,MsGPUWait,DisplayedTime,MsBetweenPresents,MsUntilDisplayed")


def pm2_row(t, ft, app="LyraGame.exe", swap="0xA", gpu_busy="9.0", displayed="16.6"):
    return (f"{app},100,{swap},DXGI,0,0,1,Hardware: Independent Flip,{t},4.0,1.0,0.5,9.5,"
            f"{gpu_busy},1.0,{displayed},{ft},12.0")


def write(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return path


def test_presentmon_2x_maps_columns_and_rebases_time(tmp_path):
    path = write(tmp_path, "pm.csv", [PM2_HEADER, pm2_row(0.50, 10.0), pm2_row(0.51, 10.0),
                                      pm2_row(0.52, 20.0)])
    df = load_presentmon_csv(path)
    assert list(df.columns) == FRAME_COLUMNS
    assert df["time_s"].tolist() == pytest.approx([0.0, 0.01, 0.02])
    assert df["frametime_ms"].tolist() == [10.0, 10.0, 20.0]
    assert df["gpu_busy_ms"].tolist() == [9.0, 9.0, 9.0]
    assert df["gpu_time_ms"].iloc[0] == 9.5
    assert df["cpu_busy_ms"].iloc[0] == 4.0
    assert df["gpu_util_pct"].isna().all()


def test_presentmon_na_values_become_nan(tmp_path):
    path = write(tmp_path, "pm.csv", [PM2_HEADER, pm2_row(0.0, 10.0, gpu_busy="NA", displayed="NA"),
                                      pm2_row(0.01, 10.0)])
    df = load_presentmon_csv(path)
    assert math.isnan(df["gpu_busy_ms"].iloc[0])
    assert df["gpu_busy_ms"].iloc[1] == 9.0


def test_presentmon_drops_rows_without_frame_time_and_sorts(tmp_path):
    path = write(tmp_path, "pm.csv", [PM2_HEADER, pm2_row(0.02, 10.0), pm2_row(0.0, "NA"),
                                      pm2_row(0.01, 12.0)])
    df = load_presentmon_csv(path)
    assert df["frametime_ms"].tolist() == [12.0, 10.0]


def test_presentmon_cpu_start_time_in_ms_is_converted(tmp_path):
    # Timestamps spanning 20 (ms) for 20 ms of frames: must be read as ms, not s.
    path = write(tmp_path, "pm.csv", [PM2_HEADER, pm2_row(1000.0, 10.0), pm2_row(1010.0, 10.0),
                                      pm2_row(1020.0, 10.0)])
    df = load_presentmon_csv(path)
    assert df["time_s"].tolist() == pytest.approx([0.0, 0.01, 0.02])


def test_presentmon_multiple_processes_need_a_filter(tmp_path):
    path = write(tmp_path, "pm.csv", [PM2_HEADER, pm2_row(0.0, 10.0), pm2_row(0.0, 7.0, app="dwm.exe"),
                                      pm2_row(0.01, 10.0)])
    with pytest.raises(ParseError, match="several processes"):
        load_presentmon_csv(path)
    df = load_presentmon_csv(path, process_name="lyragame.exe")  # case-insensitive
    assert df["frametime_ms"].tolist() == [10.0, 10.0]
    with pytest.raises(ParseError, match="no frames for process"):
        load_presentmon_csv(path, process_name="Cyberpunk2077.exe")


def test_presentmon_keeps_the_busiest_swapchain(tmp_path):
    path = write(tmp_path, "pm.csv", [PM2_HEADER, pm2_row(0.0, 10.0), pm2_row(0.005, 50.0, swap="0xB"),
                                      pm2_row(0.01, 10.0), pm2_row(0.02, 10.0)])
    assert load_presentmon_csv(path)["frametime_ms"].tolist() == [10.0, 10.0, 10.0]


def test_presentmon_1x_format(tmp_path):
    header = ("Application,ProcessID,SwapChainAddress,Runtime,SyncInterval,PresentFlags,"
              "PresentMode,AllowsTearing,TimeInSeconds,msInPresentAPI,msUntilRenderComplete,"
              "msUntilDisplayed,Dropped,msBetweenPresents,msBetweenDisplayChange")
    rows = [f"LyraGame.exe,1,0xA,DXGI,0,0,Hardware: Independent Flip,1,{t},0.1,5,6,0,{ft},16.6"
            for t, ft in [(3.0, 16.0), (3.016, 17.0)]]
    df = load_presentmon_csv(write(tmp_path, "pm1.csv", [header, *rows]))
    assert df["time_s"].tolist() == pytest.approx([0.0, 0.016])
    assert df["frametime_ms"].tolist() == [16.0, 17.0]
    assert df["gpu_busy_ms"].isna().all()


def test_presentmon_missing_frame_time_column_is_an_error(tmp_path):
    path = write(tmp_path, "bad.csv", ["Application,CPUStartTime", "LyraGame.exe,0.0"])
    with pytest.raises(ParseError, match="MsBetweenPresents"):
        load_presentmon_csv(path)


def test_presentmon_qpc_time_is_rejected(tmp_path):
    path = write(tmp_path, "qpc.csv", ["Application,CPUStartTime,CPUStartQPC,MsBetweenPresents",
                                       "LyraGame.exe,0.0,123,10.0"])
    with pytest.raises(ParseError, match="qpc_time"):
        load_presentmon_csv(path)


def test_presentmon_reads_gzip(tmp_path):
    path = tmp_path / "pm.csv.gz"
    with gzip.open(path, "wt") as f:
        f.write("\n".join([PM2_HEADER, pm2_row(0.0, 10.0), pm2_row(0.01, 11.0)]) + "\n")
    assert load_presentmon_csv(path)["frametime_ms"].tolist() == [10.0, 11.0]


def test_missing_and_empty_files(tmp_path):
    with pytest.raises(ParseError, match="not found"):
        load_presentmon_csv(tmp_path / "nope.csv")
    (tmp_path / "empty.csv").write_text("")
    with pytest.raises(ParseError, match="empty"):
        load_presentmon_csv(tmp_path / "empty.csv")


def test_frameview_maps_time_frametime_and_gpu_util(tmp_path):
    header = "Application,ProcessID,TimeInSeconds,MsBetweenPresents,MsUntilDisplayed,GPU0Util(%)"
    path = write(tmp_path, "fv.csv", [header, "LyraGame.exe,1,12.5,16.0,5,97",
                                      "LyraGame.exe,1,12.516,17.0,5,98"])
    df = load_frameview_csv(path)
    assert df["time_s"].tolist() == pytest.approx([0.0, 0.016])
    assert df["frametime_ms"].tolist() == [16.0, 17.0]
    assert df["gpu_util_pct"].tolist() == [97, 98]


def test_nvidia_smi_with_and_without_units(tmp_path):
    with_units = write(tmp_path, "a.csv", [
        "timestamp, memory.used [MiB], utilization.gpu [%]",
        "2026/11/17 12:00:00.000, 5432 MiB, 97 %",
        "2026/11/17 12:00:00.500, 5440 MiB, 98 %",
    ])
    no_units = write(tmp_path, "b.csv", [
        "timestamp, memory.used [MiB], utilization.gpu [%]",
        "2026/11/17 12:00:00.000, 5432, 97",
        "2026/11/17 12:00:00.500, 5440, 98",
    ])
    for path in (with_units, no_units):
        df = load_gpu_telemetry_csv(path)
        assert df["time_s"].tolist() == pytest.approx([0.0, 0.5])
        assert df["vram_used_mib"].tolist() == [5432, 5440]
        assert df["gpu_util_pct"].tolist() == [97, 98]
