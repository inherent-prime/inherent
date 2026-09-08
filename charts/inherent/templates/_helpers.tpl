{{/*
Common labels applied to every resource. Resource *names* are fixed
(not templated off .Release.Name) because several env vars in the chart's
contract hardcode in-namespace hostnames (WEAVIATE_URL=http://weaviate:8080,
AWS_S3_ENDPOINT=http://minio:9000, TEMPORAL_HOST=temporal:7233 — see
.memory/azure-build-spec.md "Helm chart env contract", copied from
docker-compose*.yml) — those Service names must be stable regardless of the
helm release name.
*/}}
{{- define "inherent.labels" -}}
app.kubernetes.io/part-of: inherent
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/*
Per-component selector labels. Usage: {{ include "inherent.selectorLabels" (dict "component" "public-api") }}
*/}}
{{- define "inherent.selectorLabels" -}}
app.kubernetes.io/name: inherent
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "inherent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ .Values.serviceAccount.name }}
{{- else -}}
default
{{- end -}}
{{- end -}}
