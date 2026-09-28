{{- define "es-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "es-mcp.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "es-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "es-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "es-mcp.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "es-mcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "es-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "es-mcp.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Secret holding the impersonator password: the user's own, or one this chart creates. */}}
{{- define "es-mcp.impersonatorSecretName" -}}
{{- default (printf "%s-es" (include "es-mcp.fullname" .)) .Values.elasticsearch.impersonator.existingSecret }}
{{- end }}

{{- define "es-mcp.impersonatorSecretKey" -}}
{{- if .Values.elasticsearch.impersonator.existingSecret }}
{{- .Values.elasticsearch.impersonator.existingSecretKey }}
{{- else }}
{{- "password" }}
{{- end }}
{{- end }}

{{/* TLS Secret: the user's own, or the one cert-manager issues into. Fails if neither is set. */}}
{{- define "es-mcp.tlsSecretName" -}}
{{- if .Values.tls.existingSecret }}
{{- .Values.tls.existingSecret }}
{{- else if .Values.tls.certManager.enabled }}
{{- printf "%s-tls" (include "es-mcp.fullname" .) }}
{{- else }}
{{- fail "tls.enabled needs tls.existingSecret or tls.certManager.enabled (or set tls.enabled=false when TLS is terminated in front of the server)" }}
{{- end }}
{{- end }}

{{- define "es-mcp.portName" -}}
{{- if .Values.tls.enabled }}https{{ else }}http{{ end }}
{{- end }}

{{- define "es-mcp.servicePort" -}}
{{- if .Values.service.port }}
{{- .Values.service.port }}
{{- else if .Values.tls.enabled }}
{{- 443 }}
{{- else }}
{{- 80 }}
{{- end }}
{{- end }}

{{- define "es-mcp.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
