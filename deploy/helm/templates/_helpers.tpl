{{- define "pq.selector" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "pq.labels" -}}
{{ include "pq.selector" . }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/version: {{ .root.Values.image.tag | quote }}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version }}
{{- end }}

{{- define "pq.image" -}}
{{- $image := .root.Values.image -}}
image: "{{ required "image.registry is required" $image.registry }}/pulsequeue/{{ .name }}:{{ required "image.tag is required" $image.tag }}"
imagePullPolicy: {{ $image.pullPolicy }}
{{- end }}

{{- define "pq.podSecurity" -}}
runAsNonRoot: true
runAsUser: {{ . }}
runAsGroup: {{ . }}
fsGroup: {{ . }}
seccompProfile: { type: RuntimeDefault }
{{- end }}

{{- define "pq.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: { drop: [ALL] }
{{- end }}

{{- define "pq.spread" -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "pq.selector" . | nindent 8 }}
{{- end }}

{{- define "pq.databaseEnv" -}}
{{- $db := .Values.database -}}
{{- if eq (empty $db.secretArn) (empty $db.existingSecret) -}}
{{- fail "set exactly one of database.secretArn and database.existingSecret" -}}
{{- end -}}
- name: PGHOST
  value: {{ required "database.host is required" $db.host | quote }}
- name: PGDATABASE
  value: {{ $db.name | quote }}
- name: PGUSER
  value: {{ $db.user | quote }}
{{- if $db.secretArn }}
- name: DB_SECRET_ARN
  value: {{ $db.secretArn | quote }}
- name: PGSSLMODE
  value: verify-full
- name: PGSSLROOTCERT
  value: /etc/ssl/rds-ca.pem
{{- else }}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ $db.existingSecret }}
      key: password
{{- end }}
{{- end }}

{{- define "pq.pdb" -}}
# maxUnavailable, not minAvailable: it can never block a node drain outright.
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ .root.Release.Name }}-{{ .component }}
  labels:
    {{- include "pq.labels" . | nindent 4 }}
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      {{- include "pq.selector" . | nindent 6 }}
{{- end }}

{{- define "pq.hpa" -}}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ .root.Release.Name }}-{{ .component }}
  labels:
    {{- include "pq.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ .root.Release.Name }}-{{ .component }}
  minReplicas: {{ .hpa.minReplicas }}
  maxReplicas: {{ .hpa.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .hpa.targetCPUUtilizationPercentage }}
{{- end }}
