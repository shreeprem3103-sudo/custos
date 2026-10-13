-- ---------------------------------------------------------------------------
-- CUSTOS — Crisis Digital Twin schema (PostgreSQL 14+)
--
-- GENERATED FILE. Do not edit by hand: run
--     python backend/scripts/export_schema.py
-- after changing custos/infrastructure/models.py, and commit the result.
-- `python backend/scripts/export_schema.py --check` fails the build if this file
-- and the ORM models have drifted apart.
--
-- Design notes that the DDL cannot express on its own:
--   * audit_log is an append-only hash chain. Never UPDATE or DELETE a row;
--     the application verifies seq + prev_hash + hash on every health check.
--   * sensor_readings.dedupe_key is UNIQUE by construction. A store-and-forward
--     gateway replaying a batch is a no-op, so a retry cannot double-count a flood.
--   * simulations carries the full request, KPI set, uncertainty band, timeline,
--     cascade, recommendations and explainability. A run is reproducible from the
--     row alone, with no dependency on this application's memory.
--   * cascade_edges and simulations have ON DELETE CASCADE: deleting a scenario
--     removes its findings, never the audit trail.
-- ---------------------------------------------------------------------------

-- ai_invocations
CREATE TABLE ai_invocations (
	id VARCHAR(64) NOT NULL, 
	thread_id VARCHAR(64), 
	stage VARCHAR(24) NOT NULL, 
	model VARCHAR(96) NOT NULL, 
	prompt_digest VARCHAR(64) NOT NULL, 
	prompt_tokens INTEGER NOT NULL, 
	completion_tokens INTEGER NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	ok BOOLEAN NOT NULL, 
	fell_back BOOLEAN NOT NULL, 
	grounded BOOLEAN NOT NULL, 
	rejected_claims JSON NOT NULL, 
	evidence_ids JSON NOT NULL, 
	error TEXT, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_ai_invocations_thread_id ON ai_invocations (thread_id);

-- assets
CREATE TABLE assets (
	id VARCHAR(64) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	asset_type VARCHAR(32) NOT NULL, 
	subtype VARCHAR(48), 
	criticality FLOAT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	lon FLOAT NOT NULL, 
	lat FLOAT NOT NULL, 
	elevation_m FLOAT, 
	geometry JSON NOT NULL, 
	zone VARCHAR(64), 
	operator VARCHAR(160), 
	flood_depth_m FLOAT NOT NULL, 
	power_available BOOLEAN NOT NULL, 
	access_available BOOLEAN NOT NULL, 
	service_level FLOAT NOT NULL, 
	exposure_score FLOAT NOT NULL, 
	capacity JSON NOT NULL, 
	attributes JSON NOT NULL, 
	state_observed_at TIMESTAMP WITH TIME ZONE, 
	state_source VARCHAR(64), 
	state_confidence FLOAT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_asset_type_status ON assets (asset_type, status);
CREATE INDEX ix_asset_zone ON assets (zone);
CREATE INDEX ix_assets_asset_type ON assets (asset_type);
CREATE INDEX ix_assets_lat ON assets (lat);
CREATE INDEX ix_assets_lon ON assets (lon);
CREATE INDEX ix_assets_status ON assets (status);

-- audit_log
CREATE TABLE audit_log (
	seq SERIAL NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	actor VARCHAR(64) NOT NULL, 
	role VARCHAR(24) NOT NULL, 
	action VARCHAR(32) NOT NULL, 
	resource VARCHAR(200) NOT NULL, 
	outcome VARCHAR(16) NOT NULL, 
	detail JSON NOT NULL, 
	hash VARCHAR(64) NOT NULL, 
	prev_hash VARCHAR(64) NOT NULL, 
	correlation_id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (seq), 
	UNIQUE (id)
);
CREATE INDEX ix_audit_log_action ON audit_log (action);
CREATE INDEX ix_audit_log_at ON audit_log (at);

-- checkpoint_mirror
CREATE TABLE checkpoint_mirror (
	id VARCHAR(64) NOT NULL, 
	thread_id VARCHAR(64) NOT NULL, 
	checkpoint_id VARCHAR(64) NOT NULL, 
	step INTEGER NOT NULL, 
	node VARCHAR(64) NOT NULL, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	state_digest VARCHAR(64) NOT NULL, 
	payload JSON NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_checkpoint_mirror_at ON checkpoint_mirror (at);
CREATE INDEX ix_checkpoint_mirror_thread_id ON checkpoint_mirror (thread_id);

-- connector_runs
CREATE TABLE connector_runs (
	id VARCHAR(64) NOT NULL, 
	connector VARCHAR(64) NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	ok BOOLEAN NOT NULL, 
	rows_in INTEGER NOT NULL, 
	rows_new INTEGER NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	error TEXT, 
	degraded_mode BOOLEAN NOT NULL, 
	detail JSON NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_connector_runs_connector ON connector_runs (connector);
CREATE INDEX ix_connector_runs_started_at ON connector_runs (started_at);

-- events
CREATE TABLE events (
	id VARCHAR(64) NOT NULL, 
	kind VARCHAR(24) NOT NULL, 
	topic VARCHAR(96) NOT NULL, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	payload JSON NOT NULL, 
	correlation_id VARCHAR(64) NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	seq INTEGER NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_event_kind_at ON events (kind, at);
CREATE INDEX ix_events_at ON events (at);
CREATE INDEX ix_events_kind ON events (kind);
CREATE INDEX ix_events_topic ON events (topic);

-- incidents
CREATE TABLE incidents (
	id VARCHAR(64) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	category VARCHAR(32) NOT NULL, 
	severity INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	confidence FLOAT NOT NULL, 
	summary TEXT NOT NULL, 
	lon FLOAT NOT NULL, 
	lat FLOAT NOT NULL, 
	radius_m FLOAT NOT NULL, 
	zone VARCHAR(64), 
	population_exposed INTEGER NOT NULL, 
	reported_by VARCHAR(64) NOT NULL, 
	detected_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	explainability JSON NOT NULL, 
	attributes JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_incident_status_sev ON incidents (status, severity);
CREATE INDEX ix_incidents_category ON incidents (category);
CREATE INDEX ix_incidents_status ON incidents (status);

-- resources
CREATE TABLE resources (
	id VARCHAR(64) NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	resource_type VARCHAR(32) NOT NULL, 
	home_asset_id VARCHAR(64), 
	quantity FLOAT NOT NULL, 
	available FLOAT NOT NULL, 
	capabilities JSON NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	attributes JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_resources_resource_type ON resources (resource_type);

-- simulations
CREATE TABLE simulations (
	id VARCHAR(64) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	description TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	created_by VARCHAR(64) NOT NULL, 
	baseline_id VARCHAR(64), 
	horizon_minutes INTEGER NOT NULL, 
	timestep_minutes INTEGER NOT NULL, 
	ensemble_size INTEGER NOT NULL, 
	random_seed INTEGER NOT NULL, 
	request JSON NOT NULL, 
	result JSON NOT NULL, 
	kpis JSON NOT NULL, 
	uncertainty JSON NOT NULL, 
	timeline JSON NOT NULL, 
	cascade JSON NOT NULL, 
	recommendations JSON NOT NULL, 
	explainability JSON NOT NULL, 
	sensitivity JSON NOT NULL, 
	narrative TEXT NOT NULL, 
	determinism_token VARCHAR(64) NOT NULL, 
	degraded BOOLEAN NOT NULL, 
	runtime_ms INTEGER NOT NULL, 
	error TEXT, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_sim_status_created ON simulations (status, created_at);
CREATE INDEX ix_simulations_status ON simulations (status);

-- system_state
CREATE TABLE system_state (
	key VARCHAR(64) NOT NULL, 
	value JSON NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (key)
);

-- tasks
CREATE TABLE tasks (
	id VARCHAR(64) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	description TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	priority INTEGER NOT NULL, 
	due_at TIMESTAMP WITH TIME ZONE, 
	incident_id VARCHAR(64), 
	simulation_id VARCHAR(64), 
	recommendation_id VARCHAR(64), 
	assigned_to VARCHAR(64), 
	target_asset_id VARCHAR(64), 
	resource_requirements JSON NOT NULL, 
	created_by VARCHAR(64) NOT NULL, 
	history JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_task_status_priority ON tasks (status, priority);
CREATE INDEX ix_tasks_incident_id ON tasks (incident_id);
CREATE INDEX ix_tasks_status ON tasks (status);

-- zones
CREATE TABLE zones (
	id VARCHAR(64) NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	lon FLOAT NOT NULL, 
	lat FLOAT NOT NULL, 
	population INTEGER NOT NULL, 
	vulnerability_index FLOAT NOT NULL, 
	children_pct FLOAT NOT NULL, 
	elderly_pct FLOAT NOT NULL, 
	population_density FLOAT NOT NULL, 
	mean_elevation_m FLOAT NOT NULL, 
	attributes JSON NOT NULL, 
	PRIMARY KEY (id)
);

-- cascade_edges
CREATE TABLE cascade_edges (
	id VARCHAR(64) NOT NULL, 
	simulation_id VARCHAR(64) NOT NULL, 
	timestep_min INTEGER NOT NULL, 
	cause_asset_id VARCHAR(64) NOT NULL, 
	target_asset_id VARCHAR(64) NOT NULL, 
	mechanism VARCHAR(64) NOT NULL, 
	relation_kind VARCHAR(24), 
	hop INTEGER NOT NULL, 
	magnitude FLOAT NOT NULL, 
	probability FLOAT NOT NULL, 
	status_after VARCHAR(16) NOT NULL, 
	evidence_ids JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(simulation_id) REFERENCES simulations (id) ON DELETE CASCADE
);
CREATE INDEX ix_cascade_edges_simulation_id ON cascade_edges (simulation_id);
CREATE INDEX ix_cascade_sim_ts ON cascade_edges (simulation_id, timestep_min);

-- facility_details
CREATE TABLE facility_details (
	asset_id VARCHAR(64) NOT NULL, 
	capacity JSON NOT NULL, 
	autonomy_hours FLOAT NOT NULL, 
	oxygen_hours FLOAT NOT NULL, 
	trauma_level INTEGER NOT NULL, 
	beds_total INTEGER NOT NULL, 
	beds_occupied INTEGER NOT NULL, 
	vulnerable_patients INTEGER NOT NULL, 
	accepts_evacuees BOOLEAN NOT NULL, 
	helipad_elevation_m FLOAT, 
	PRIMARY KEY (asset_id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id) ON DELETE CASCADE
);

-- incident_assets
CREATE TABLE incident_assets (
	id VARCHAR(64) NOT NULL, 
	incident_id VARCHAR(64) NOT NULL, 
	asset_id VARCHAR(64) NOT NULL, 
	role VARCHAR(24) NOT NULL, 
	impact FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_incident_asset UNIQUE (incident_id, asset_id), 
	FOREIGN KEY(incident_id) REFERENCES incidents (id) ON DELETE CASCADE, 
	FOREIGN KEY(asset_id) REFERENCES assets (id) ON DELETE CASCADE
);

-- incident_events
CREATE TABLE incident_events (
	id VARCHAR(64) NOT NULL, 
	incident_id VARCHAR(64) NOT NULL, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	stage VARCHAR(24) NOT NULL, 
	actor VARCHAR(64) NOT NULL, 
	detail TEXT NOT NULL, 
	evidence_ids JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(incident_id) REFERENCES incidents (id) ON DELETE CASCADE
);
CREATE INDEX ix_incident_events_incident_id ON incident_events (incident_id);

-- incident_updates
CREATE TABLE incident_updates (
	id VARCHAR(64) NOT NULL, 
	incident_id VARCHAR(64) NOT NULL, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	author VARCHAR(64) NOT NULL, 
	kind VARCHAR(24) NOT NULL, 
	body TEXT NOT NULL, 
	payload JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(incident_id) REFERENCES incidents (id) ON DELETE CASCADE
);
CREATE INDEX ix_incident_updates_incident_id ON incident_updates (incident_id);

-- recommendations
CREATE TABLE recommendations (
	id VARCHAR(64) NOT NULL, 
	simulation_id VARCHAR(64), 
	incident_id VARCHAR(64), 
	rank INTEGER NOT NULL, 
	action VARCHAR(200) NOT NULL, 
	intervention_type VARCHAR(32) NOT NULL, 
	target_id VARCHAR(64), 
	"window" VARCHAR(64) NOT NULL, 
	rationale TEXT NOT NULL, 
	expected_kpi_delta JSON NOT NULL, 
	resources_required JSON NOT NULL, 
	urgency INTEGER NOT NULL, 
	confidence FLOAT NOT NULL, 
	feasibility FLOAT NOT NULL, 
	evidence_ids JSON NOT NULL, 
	counterfactual TEXT NOT NULL, 
	generated_by VARCHAR(48) NOT NULL, 
	accepted BOOLEAN, 
	accepted_by VARCHAR(64), 
	accepted_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(simulation_id) REFERENCES simulations (id) ON DELETE SET NULL, 
	FOREIGN KEY(incident_id) REFERENCES incidents (id) ON DELETE SET NULL
);
CREATE INDEX ix_recommendations_incident_id ON recommendations (incident_id);
CREATE INDEX ix_recommendations_simulation_id ON recommendations (simulation_id);

-- relationships
CREATE TABLE relationships (
	id VARCHAR(64) NOT NULL, 
	source_id VARCHAR(64) NOT NULL, 
	target_id VARCHAR(64) NOT NULL, 
	kind VARCHAR(24) NOT NULL, 
	criticality FLOAT NOT NULL, 
	lead_time_min INTEGER NOT NULL, 
	attenuation FLOAT NOT NULL, 
	failure_mode VARCHAR(16) NOT NULL, 
	recovery_hours FLOAT NOT NULL, 
	redundancy_group VARCHAR(64), 
	active BOOLEAN NOT NULL, 
	attributes JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_relationship UNIQUE (source_id, target_id, kind), 
	FOREIGN KEY(source_id) REFERENCES assets (id) ON DELETE CASCADE, 
	FOREIGN KEY(target_id) REFERENCES assets (id) ON DELETE CASCADE
);
CREATE INDEX ix_rel_source_kind ON relationships (source_id, kind);
CREATE INDEX ix_rel_target_kind ON relationships (target_id, kind);
CREATE INDEX ix_relationships_kind ON relationships (kind);

-- resource_assignments
CREATE TABLE resource_assignments (
	id VARCHAR(64) NOT NULL, 
	resource_id VARCHAR(64) NOT NULL, 
	task_id VARCHAR(64), 
	simulation_id VARCHAR(64), 
	quantity FLOAT NOT NULL, 
	from_asset_id VARCHAR(64), 
	to_asset_id VARCHAR(64), 
	depart_min INTEGER NOT NULL, 
	eta_min INTEGER, 
	route_asset_ids JSON NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(resource_id) REFERENCES resources (id) ON DELETE CASCADE
);

-- road_details
CREATE TABLE road_details (
	asset_id VARCHAR(64) NOT NULL, 
	length_m FLOAT NOT NULL, 
	lanes INTEGER NOT NULL, 
	free_flow_speed_kmh FLOAT NOT NULL, 
	flood_threshold_m FLOAT NOT NULL, 
	heavy_threshold_m FLOAT NOT NULL, 
	is_evacuation_route BOOLEAN NOT NULL, 
	is_bridge BOOLEAN NOT NULL, 
	underpass BOOLEAN NOT NULL, 
	altitude_profile JSON NOT NULL, 
	PRIMARY KEY (asset_id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id) ON DELETE CASCADE
);

-- sensors
CREATE TABLE sensors (
	id VARCHAR(64) NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	metric VARCHAR(64) NOT NULL, 
	unit VARCHAR(24) NOT NULL, 
	provider VARCHAR(64) NOT NULL, 
	source_ref VARCHAR(160), 
	asset_id VARCHAR(64), 
	zone VARCHAR(64), 
	lon FLOAT NOT NULL, 
	lat FLOAT NOT NULL, 
	elevation_m FLOAT, 
	expected_min FLOAT, 
	expected_max FLOAT, 
	baseline_median FLOAT, 
	baseline_mad FLOAT, 
	sample_count INTEGER NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	attributes JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id) ON DELETE SET NULL
);
CREATE INDEX ix_sensors_asset_id ON sensors (asset_id);
CREATE INDEX ix_sensors_metric ON sensors (metric);
CREATE INDEX ix_sensors_zone ON sensors (zone);

-- shelter_details
CREATE TABLE shelter_details (
	asset_id VARCHAR(64) NOT NULL, 
	capacity_people INTEGER NOT NULL, 
	occupancy INTEGER NOT NULL, 
	has_water BOOLEAN NOT NULL, 
	has_power_backup BOOLEAN NOT NULL, 
	accessible_for_disability BOOLEAN NOT NULL, 
	opening_time_min INTEGER NOT NULL, 
	PRIMARY KEY (asset_id), 
	FOREIGN KEY(asset_id) REFERENCES assets (id) ON DELETE CASCADE
);

-- simulation_events
CREATE TABLE simulation_events (
	id VARCHAR(64) NOT NULL, 
	simulation_id VARCHAR(64) NOT NULL, 
	seq INTEGER NOT NULL, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	stage VARCHAR(24) NOT NULL, 
	progress FLOAT NOT NULL, 
	message TEXT NOT NULL, 
	payload JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(simulation_id) REFERENCES simulations (id) ON DELETE CASCADE
);
CREATE INDEX ix_simulation_events_simulation_id ON simulation_events (simulation_id);

-- anomalies
CREATE TABLE anomalies (
	id VARCHAR(64) NOT NULL, 
	sensor_id VARCHAR(64) NOT NULL, 
	metric VARCHAR(64) NOT NULL, 
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	observed_value FLOAT NOT NULL, 
	expected_value FLOAT NOT NULL, 
	z_score FLOAT NOT NULL, 
	severity VARCHAR(12) NOT NULL, 
	method VARCHAR(32) NOT NULL, 
	explanation TEXT NOT NULL, 
	confidence FLOAT NOT NULL, 
	acknowledged BOOLEAN NOT NULL, 
	promoted_incident_id VARCHAR(64), 
	evidence JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(sensor_id) REFERENCES sensors (id) ON DELETE CASCADE, 
	FOREIGN KEY(promoted_incident_id) REFERENCES incidents (id) ON DELETE SET NULL
);
CREATE INDEX ix_anomalies_observed_at ON anomalies (observed_at);
CREATE INDEX ix_anomalies_sensor_id ON anomalies (sensor_id);
CREATE INDEX ix_anomalies_severity ON anomalies (severity);
CREATE INDEX ix_anomaly_time ON anomalies (observed_at);

-- sensor_readings
CREATE TABLE sensor_readings (
	id VARCHAR(64) NOT NULL, 
	sensor_id VARCHAR(64) NOT NULL, 
	metric VARCHAR(64) NOT NULL, 
	value FLOAT NOT NULL, 
	unit VARCHAR(24) NOT NULL, 
	quality FLOAT NOT NULL, 
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	ingested_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	dedupe_key VARCHAR(64) NOT NULL, 
	is_outlier BOOLEAN NOT NULL, 
	raw JSON NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_reading_dedupe UNIQUE (dedupe_key), 
	FOREIGN KEY(sensor_id) REFERENCES sensors (id) ON DELETE CASCADE
);
CREATE INDEX ix_reading_sensor_time ON sensor_readings (sensor_id, observed_at);
CREATE INDEX ix_sensor_readings_observed_at ON sensor_readings (observed_at);
CREATE INDEX ix_sensor_readings_sensor_id ON sensor_readings (sensor_id);

