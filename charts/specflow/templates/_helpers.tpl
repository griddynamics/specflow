{{- define "specflow.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "specflow.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "specflow.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "specflow.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "specflow.selectorLabels" -}}
app.kubernetes.io/name: {{ include "specflow.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .Values.component }}
{{- end -}}

{{- define "specflow.labels" -}}
helm.sh/chart: {{ include "specflow.chart" . }}
{{ include "specflow.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "specflow.image" -}}
{{- $registry := trimSuffix "/" (.Values.image.registry | default "") -}}
{{- $prefix := ternary (printf "%s/" $registry) "" (ne $registry "") -}}
{{- if .Values.image.digest -}}
{{- printf "%s%s@%s" $prefix .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s%s:%s" $prefix .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

{{- define "specflow.serviceAccountName" -}}
{{- if .Values.serviceAccount.name -}}
{{- tpl .Values.serviceAccount.name . -}}
{{- else if .Values.serviceAccount.create -}}
{{- include "specflow.fullname" . -}}
{{- else -}}
default
{{- end -}}
{{- end -}}

{{- define "specflow.persistenceName" -}}
{{- printf "%s-%s" (include "specflow.fullname" .) .Values.persistence.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "specflow.persistenceClaimName" -}}
{{- .Values.persistence.existingClaim | default (include "specflow.persistenceName" .) -}}
{{- end -}}

{{- define "specflow.primaryServicePort" -}}
{{- (index .Values.service.ports 0).port -}}
{{- end -}}

{{- define "specflow.extraObjects" -}}
{{- range $object := .Values.extraObjects }}
---
{{- if kindIs "map" $object }}
{{ tpl (toYaml $object) $ }}
{{- else if kindIs "string" $object }}
{{ tpl $object $ }}
{{- else }}
{{ fail "extraObjects entries must be YAML objects or strings" }}
{{- end }}
{{- end }}
{{- end -}}

