"""Agent modules. To add one: create agents/<name>.py with an Agent subclass and add it to ALL below."""
from .demo import Demo
from .alert_triage import AlertTriage
from .sentinel import Sentinel
from .backup_warden import BackupWarden
from .capacity_planner import CapacityPlanner
from .patch_scout import PatchScout
from .config_drift import ConfigDrift
from .commander import Commander
from .archivist import Archivist
from .critic import Critic
from .ids_doctor import IdsDoctor
from .dns_watcher import DnsWatcher
from .exposure_auditor import ExposureAuditor
from .iot_watcher import IotWatcher
from .day_briefing import DayBriefing
from .briefing_editor import BriefingEditor
from .research_librarian import ResearchLibrarian
from .backlog_keeper import BacklogKeeper
from .media_curator import MediaCurator
from .job_scout import JobScout
from .correlator import Correlator
from .watchdog import Watchdog

ALL = [Demo, AlertTriage, Sentinel, BackupWarden, CapacityPlanner, PatchScout, ConfigDrift, Commander, Archivist, Critic, IdsDoctor, DnsWatcher, ExposureAuditor, IotWatcher, DayBriefing, BriefingEditor, ResearchLibrarian, BacklogKeeper, MediaCurator, JobScout, Correlator, Watchdog]
