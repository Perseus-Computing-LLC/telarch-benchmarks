//! Machine description for the JSON reports, read when the program runs.

use std::env::consts::{ARCH, OS};

/// `Linux <arch>, <CPU model>, <n> logical CPUs`, as `scripts/bench_env.py` writes it;
/// only the OS and architecture where there is no `/proc/cpuinfo`.
pub fn describe() -> String {
    std::fs::read_to_string("/proc/cpuinfo")
        .map_or_else(|_| format!("{OS} {ARCH}"), |info| from_cpuinfo(&info))
}

fn from_cpuinfo(info: &str) -> String {
    let model = info
        .lines()
        .find_map(|l| l.strip_prefix("model name")?.split_once(':'))
        .map_or_else(
            || "unknown CPU".into(),
            |(_, m)| m.replace("(R)", "").replace("(TM)", ""),
        );
    let model = model.split_whitespace().collect::<Vec<_>>().join(" ");
    let cpus = info.lines().filter(|l| l.starts_with("processor")).count();
    format!("Linux {ARCH}, {model}, {cpus} logical CPUs")
}

#[cfg(test)]
mod tests {
    #[test]
    fn names_model_and_logical_cpus() {
        let entry = "processor\t: 0\nmodel name\t: 13th Gen Intel(R) Core(TM) i5-13500\n\n";
        assert_eq!(
            super::from_cpuinfo(&entry.repeat(2)),
            format!(
                "Linux {}, 13th Gen Intel Core i5-13500, 2 logical CPUs",
                super::ARCH
            )
        );
    }
}
