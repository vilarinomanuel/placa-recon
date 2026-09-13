/* placa-recon · panel de control
   Cliente sin dependencias de build: enrutado por hash, consumo de la API
   FastAPI y flujo SSE para las detecciones en vivo. */

const API = '__PORT_8000__'.startsWith('__') ? '' : '__PORT_8000__';

const el = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat('es-ES');

const VISTAS = {
  panel: { titulo: 'Panel', sub: 'Estado general del reconocimiento' },
  camaras: { titulo: 'Cámaras', sub: 'Administración de fuentes y servicios' },
  detecciones: { titulo: 'Detecciones', sub: 'Historial de matrículas registradas' },
  sistema: { titulo: 'Sistema', sub: 'Configuración efectiva y despliegue' },
};

const estado = {
  vista: 'panel',
  horas: 24,
  camaras: [],
  controlHabilitado: false,
  filtros: { placa: '', camara: '', min: 0, desde: '' },
  pagina: 0,
  limite: 25,
  total: 0,
  editando: null,
  fuenteOculta: null,
  graficos: {},
};

/* ------------------------------------------------------------------ utilidades */
async function api(ruta, opciones = {}) {
  const res = await fetch(`${API}/api${ruta}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opciones,
  });
  if (!res.ok) {
    let detalle = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j.detail) detalle = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch { /* respuesta sin JSON */ }
    throw new Error(detalle);
  }
  return res.status === 204 ? null : res.json();
}

function aviso(mensaje, tipo = '') {
  const nodo = document.createElement('div');
  nodo.className = `toast ${tipo}`;
  nodo.textContent = mensaje;
  el('avisos').append(nodo);
  setTimeout(() => nodo.remove(), 4200);
}

const escapar = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

function fechaCorta(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('es-ES', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function hace(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return `hace ${Math.round(s)} s`;
  if (s < 3600) return `hace ${Math.round(s / 60)} min`;
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`;
  return `hace ${Math.round(s / 86400)} d`;
}

const nombreCamara = (id) => estado.camaras.find((c) => c.id === id)?.nombre || id || '—';

function claseConf(valor) {
  if (valor >= 0.92) return '';
  if (valor >= 0.85) return 'media';
  return 'baja';
}

function urlImagen(ruta) {
  return `${API}/api/imagen?ruta=${encodeURIComponent(ruta)}`;
}

/* --------------------------------------------------------------------- rutas */
function irA(vista) {
  if (!VISTAS[vista]) vista = 'panel';
  estado.vista = vista;
  document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('activa', a.dataset.vista === vista));
  document.querySelectorAll('.vista').forEach((s) => s.classList.add('oculta'));
  el(`vista-${vista}`).classList.remove('oculta');
  el('titulo-vista').textContent = VISTAS[vista].titulo;
  el('sub-vista').textContent = VISTAS[vista].sub;
  el('principal').scrollTop = 0;
  if (vista === 'panel') { cargarEstado(); cargarMetricas(); cargarRecientes(); }
  if (vista === 'camaras') cargarCamaras();
  if (vista === 'detecciones') cargarDetecciones();
  if (vista === 'sistema') cargarEstado();
}

window.addEventListener('hashchange', () => irA(location.hash.replace('#/', '') || 'panel'));

/* --------------------------------------------------------------------- panel */
function esqueletoKpis() {
  el('kpis').innerHTML = Array.from({ length: 5 }, () => '<div class="esqueleto esq-kpi"></div>').join('');
}

async function cargarEstado() {
  try {
    const d = await api('/estado');
    estado.camaras = d.camaras;
    estado.controlHabilitado = d.config.control_habilitado;
    pintarKpis(d);
    pintarSistema(d);
    el('pastilla-camaras').textContent = `${d.camaras_activas}/${d.camaras_totales}`;
    el('pastilla-detecciones').textContent = fmt.format(d.detecciones_totales);
    el('nota-modo').textContent = d.config.modo_demo
      ? 'Modo demostración: datos sintéticos'
      : `Datos: ${d.config.directorio_datos}`;
    rellenarSelectorCamaras();
  } catch (e) {
    aviso(`No pude leer el estado: ${e.message}`, 'mal');
    el('kpis').innerHTML = `<p class="vacio">Sin conexión con la API. ${escapar(e.message)}</p>`;
  }
}

function pintarKpis(d) {
  const alm = d.almacenamiento;
  const tarjetas = [
    { etq: 'Cámaras activas', val: `${d.camaras_activas}/${d.camaras_totales}`, pie: d.plataforma, clase: 'vivo' },
    { etq: 'Detecciones hoy', val: fmt.format(d.detecciones_hoy), pie: `${fmt.format(d.detecciones_ultima_hora)} en la última hora`, clase: 'acento' },
    { etq: 'Total registrado', val: fmt.format(d.detecciones_totales), pie: `${fmt.format(d.placas_unicas)} placas únicas` },
    { etq: 'Confianza media', val: d.confianza_media.toFixed(3), pie: 'mínimo de min(OCR, detección)' },
    {
      etq: 'Última detección',
      val: d.ultima_deteccion ? d.ultima_deteccion.placa : '—',
      pie: d.ultima_deteccion ? `${hace(d.ultima_deteccion.momento)} · ${nombreCamara(d.ultima_deteccion.camara)}` : 'sin registros',
    },
  ];
  if (alm) tarjetas.push({ etq: 'Almacenamiento', val: `${alm.usado_pct}%`, pie: `${alm.libre_gb} GB libres de ${alm.total_gb} GB` });

  el('kpis').innerHTML = tarjetas.map((t) => `
    <div class="kpi ${t.clase || ''}">
      <span class="etiqueta">${escapar(t.etq)}</span>
      <span class="valor">${escapar(t.val)}</span>
      <span class="pie">${escapar(t.pie)}</span>
    </div>`).join('');
}

const COLORES = ['#f2a33c', '#5fd4c4', '#8fa8d8', '#e0664f', '#c9a0dc', '#a3abb7'];

const ICONOS = {
  lapiz: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 17.2 15.1 6.1l2.8 2.8L6.8 20H4zM16.5 4.7l1.4-1.4a1.4 1.4 0 0 1 2 0l1.3 1.3a1.4 1.4 0 0 1 0 2l-1.4 1.4z"/></svg>',
  engranaje: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 9.2A2.8 2.8 0 1 0 12 14.8A2.8 2.8 0 0 0 12 9.2m7.4 1.7-1.6-.3-.5-1.3.9-1.4-1.7-1.7-1.4.9-1.3-.5-.3-1.6h-2.4l-.3 1.6-1.3.5-1.4-.9L4.4 7.9l.9 1.4-.5 1.3-1.6.3v2.4l1.6.3.5 1.3-.9 1.4 1.7 1.7 1.4-.9 1.3.5.3 1.6h2.4l.3-1.6 1.3-.5 1.4.9 1.7-1.7-.9-1.4.5-1.3 1.6-.3z"/></svg>',
  papelera: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 8h10l-.8 12H7.8zM9.5 4.5h5L15.5 6H19v2H5V6h3.5z"/></svg>',
};

async function cargarMetricas() {
  try {
    const m = await api(`/metricas?horas=${estado.horas}`);
    pintarGraficoHoras(m);
    pintarGraficoConfianza(m);
    pintarTopPlacas(m);
  } catch (e) {
    aviso(`No pude calcular las métricas: ${e.message}`, 'mal');
  }
}

function etiquetasHoras(m) {
  return m.por_hora.map((p) => {
    const [dia, hora] = p.hora.split(' ');
    return `${dia.slice(8)}/${dia.slice(5, 7)} ${hora.slice(0, 2)}h`;
  });
}

const baseEjes = {
  grid: { color: 'rgba(255,255,255,.055)', drawTicks: false },
  border: { display: false },
  ticks: { color: '#7f8792', font: { family: 'JetBrains Mono', size: 10 }, maxRotation: 0, autoSkipPadding: 18 },
};

function pintarGraficoHoras(m) {
  const etiquetas = etiquetasHoras(m);
  const camaras = Object.keys(m.por_camara_hora);
  const series = camaras.length
    ? camaras.map((id, i) => ({
      label: nombreCamara(id),
      data: m.por_camara_hora[id],
      backgroundColor: COLORES[i % COLORES.length],
      borderRadius: 2,
      borderSkipped: false,
      barPercentage: 0.92,
      categoryPercentage: 0.88,
    }))
    : [{ label: 'Detecciones', data: m.por_hora.map((p) => p.total), backgroundColor: COLORES[0] }];

  estado.graficos.horas?.destroy();
  estado.graficos.horas = new Chart(el('gr-horas'), {
    type: 'bar',
    data: { labels: etiquetas, datasets: series },
    options: {
      maintainAspectRatio: false,
      animation: { duration: 320 },
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ...baseEjes, stacked: true },
        y: { ...baseEjes, stacked: true, beginAtZero: true, ticks: { ...baseEjes.ticks, precision: 0 } },
      },
      plugins: {
        legend: { position: 'bottom', labels: { color: '#a3abb7', boxWidth: 9, boxHeight: 9, usePointStyle: true, pointStyle: 'circle', font: { family: 'Inter', size: 11 } } },
        tooltip: tooltipOscuro(),
      },
    },
  });
}

function pintarGraficoConfianza(m) {
  estado.graficos.conf?.destroy();
  estado.graficos.conf = new Chart(el('gr-confianza'), {
    type: 'bar',
    data: {
      labels: m.confianza.map((c) => c.rango),
      datasets: [{
        label: 'Detecciones',
        data: m.confianza.map((c) => c.total),
        backgroundColor: ['#e0664f', '#f2a33c', '#8fa8d8', '#5fd4c4'],
        borderRadius: 3,
      }],
    },
    options: {
      indexAxis: 'y',
      maintainAspectRatio: false,
      animation: { duration: 320 },
      scales: {
        x: { ...baseEjes, beginAtZero: true, ticks: { ...baseEjes.ticks, precision: 0 } },
        y: { ...baseEjes, grid: { display: false } },
      },
      plugins: { legend: { display: false }, tooltip: tooltipOscuro() },
    },
  });
}

function tooltipOscuro() {
  return {
    backgroundColor: '#1f242c',
    borderColor: '#333b46',
    borderWidth: 1,
    titleColor: '#e7eaef',
    bodyColor: '#a3abb7',
    titleFont: { family: 'Inter', size: 12 },
    bodyFont: { family: 'JetBrains Mono', size: 11 },
    padding: 10,
    displayColors: true,
    boxWidth: 8,
    boxHeight: 8,
    usePointStyle: true,
  };
}

function pintarTopPlacas(m) {
  if (!m.top_placas.length) {
    el('top-placas').innerHTML = '<li class="sub">Sin detecciones en el rango.</li>';
    return;
  }
  const max = m.top_placas[0].total;
  el('top-placas').innerHTML = `<li class="sub">Placas más frecuentes (${estado.horas} h)</li>` +
    m.top_placas.slice(0, 5).map((p) => `
      <li>
        <span class="placa">${escapar(p.placa)}</span>
        <span class="barra"><i style="width:${(100 * p.total / max).toFixed(1)}%"></i></span>
        <span class="n">${p.total}</span>
      </li>`).join('');
}

async function cargarRecientes() {
  try {
    const d = await api('/detecciones?limite=14');
    pintarRecientes(d.detecciones);
  } catch (e) {
    el('tira-recientes').innerHTML = `<p class="vacio">${escapar(e.message)}</p>`;
  }
}

function pintarRecientes(filas) {
  if (!filas.length) {
    el('tira-recientes').innerHTML = '<p class="vacio">Todavía no hay detecciones registradas.</p>';
    return;
  }
  el('tira-recientes').innerHTML = filas.map((f) => tarjetaCaptura(f)).join('');
}

function tarjetaCaptura(f) {
  const img = f.recorte
    ? `<img src="${urlImagen(f.recorte)}" alt="Recorte de ${escapar(f.placa)}" loading="lazy">`
    : '<img alt="" aria-hidden="true">';
  return `
    <button class="captura" type="button" data-recorte="${escapar(f.recorte)}" data-placa="${escapar(f.placa)}" data-momento="${escapar(f.momento)}">
      ${img}
      <span class="cap-info">
        <span class="cap-placa">${escapar(f.placa)}</span>
        <span class="cap-meta">${escapar(hace(f.momento))} · ${escapar(nombreCamara(f.camara))}</span>
        <span class="cap-meta">confianza ${f.confianza.toFixed(2)}</span>
      </span>
    </button>`;
}

/* ------------------------------------------------------------------- cámaras */
async function cargarCamaras() {
  el('rejilla-camaras').innerHTML = Array.from({ length: 3 }, () => '<div class="esqueleto" style="height:196px;border-radius:14px"></div>').join('');
  try {
    const d = await api('/camaras');
    estado.camaras = d.camaras;
    estado.controlHabilitado = d.control_habilitado;
    pintarCamaras();
    rellenarSelectorCamaras();
  } catch (e) {
    el('rejilla-camaras').innerHTML = `<p class="vacio">${escapar(e.message)}</p>`;
  }
}

function pintarCamaras() {
  const activas = estado.camaras.filter((c) => c.servicio.activa).length;
  el('resumen-camaras').textContent = `${estado.camaras.length} cámaras configuradas · ${activas} en ejecución`;

  const control = el('aviso-control');
  if (estado.controlHabilitado) {
    control.classList.add('oculta');
  } else {
    control.classList.remove('oculta');
    control.textContent = 'Control de servicios deshabilitado. Arranca el panel con ALPR_WEB_ALLOW_CONTROL=true para iniciar o detener unidades desde aquí.';
  }

  if (!estado.camaras.length) {
    el('rejilla-camaras').innerHTML = '<p class="vacio">No hay cámaras configuradas. Usa «Detectar hardware» o añade una fuente RTSP.</p>';
    return;
  }

  el('rejilla-camaras').innerHTML = estado.camaras.map((c) => {
    const s = c.servicio;
    const clase = s.activa ? 'activa' : (s.estado === 'failed' ? 'fallo' : 'detenida');
    const texto = s.activa ? 'En ejecución' : (s.estado === 'failed' ? 'Fallo' : 'Detenida');
    const puede = estado.controlHabilitado;
    return `
      <article class="camara">
        <div class="camara-cab">
          <div>
            <h3>${escapar(c.nombre)}</h3>
            <p class="fuente">${escapar(/^\d+$/.test(c.fuente) ? `dispositivo local · índice ${c.fuente}` : c.fuente)}</p>
          </div>
          <span class="insignia ${clase}"><span class="punto"></span>${texto}</span>
        </div>
        <dl class="camara-datos">
          <div><dt>Detecciones</dt><dd>${fmt.format(c.detecciones)}</dd></div>
          <div><dt>Conf. mínima</dt><dd>${Number(c.min_confianza).toFixed(2)}</dd></div>
          <div><dt>FPS objetivo</dt><dd>${Number(c.fps_objetivo).toFixed(0)}</dd></div>
        </dl>
        ${c.notas ? `<p class="notas">${escapar(c.notas)}</p>` : ''}
        <p class="notas mono">${escapar(s.unidad)}${s.reinicios ? ` · ${s.reinicios} reinicios` : ''}</p>
        <div class="camara-pie">
          ${s.activa
            ? `<button class="btn fantasma" data-accion="detener" data-id="${c.id}" ${puede ? '' : 'disabled'}>Detener</button>
               <button class="btn fantasma" data-accion="reiniciar" data-id="${c.id}" ${puede ? '' : 'disabled'}>Reiniciar</button>`
            : `<button class="btn" data-accion="iniciar" data-id="${c.id}" ${puede ? '' : 'disabled'}>Iniciar</button>`}
          <span class="iconos">
            <button class="icono" data-editar="${c.id}" title="Editar cámara" aria-label="Editar cámara">${ICONOS.lapiz}</button>
            <button class="icono" data-env="${c.id}" title="Ver configuración systemd" aria-label="Ver configuración systemd">${ICONOS.engranaje}</button>
            <button class="icono peligro" data-borrar="${c.id}" title="Eliminar cámara" aria-label="Eliminar cámara">${ICONOS.papelera}</button>
          </span>
        </div>
      </article>`;
  }).join('');
}

function rellenarSelectorCamaras() {
  const sel = el('f-camara');
  const actual = sel.value;
  sel.innerHTML = '<option value="">Todas</option>' +
    estado.camaras.map((c) => `<option value="${escapar(c.id)}">${escapar(c.nombre)}</option>`).join('');
  sel.value = actual;
}

async function accionCamara(id, accion) {
  try {
    const r = await api(`/camaras/${id}/accion`, { method: 'POST', body: JSON.stringify({ accion }) });
    aviso(`${nombreCamara(id)}: ${accion} ${r.simulado ? '(simulado)' : 'aplicado'}`, 'ok');
    cargarCamaras();
  } catch (e) {
    aviso(e.message, 'mal');
  }
}

async function borrarCamara(id) {
  if (!confirm(`¿Eliminar «${nombreCamara(id)}» del panel? No se borra ningún dato ya registrado.`)) return;
  try {
    await api(`/camaras/${id}`, { method: 'DELETE' });
    aviso('Cámara eliminada', 'ok');
    cargarCamaras();
  } catch (e) {
    aviso(e.message, 'mal');
  }
}

async function mostrarEnv(id) {
  try {
    const d = await api(`/camaras/${id}/env`);
    el('env-ruta').textContent = `${d.ruta_sugerida} · unidad ${d.unidad}`;
    el('env-contenido').textContent = d.contenido;
    el('modal-env').showModal();
  } catch (e) {
    aviso(e.message, 'mal');
  }
}

/* --------------------------------------------------------- diálogo de cámara */
function abrirModalCamara(camara = null) {
  estado.editando = camara?.id || null;
  el('modal-titulo').textContent = camara ? `Editar ${camara.nombre}` : 'Añadir cámara';
  el('c-nombre').value = camara?.nombre || '';
  el('c-tipo').value = camara?.tipo || 'rtsp';
  const oculta = !!camara && camara.fuente.includes(':***@');
  estado.fuenteOculta = oculta ? camara.fuente : null;
  el('c-fuente').value = camara && !oculta ? camara.fuente : '';
  el('c-fuente').placeholder = oculta
    ? `${camara.fuente} (déjalo vacío para conservarla)`
    : 'rtsp://usuario:clave@192.168.1.40:554/Streaming/Channels/101';
  el('c-conf').value = camara?.min_confianza ?? 0.8;
  el('c-fps').value = camara?.fps_objetivo ?? 8;
  el('c-notas').value = camara?.notas || '';
  el('c-activa').checked = camara ? !!camara.activa : true;
  el('modal-error').classList.add('oculta');
  el('modal-camara').showModal();
  el('c-nombre').focus();
}

async function guardarCamara(evento) {
  evento.preventDefault();
  const err = el('modal-error');
  const cuerpo = {
    nombre: el('c-nombre').value.trim(),
    fuente: el('c-fuente').value.trim() || estado.fuenteOculta || '',
    tipo: el('c-tipo').value,
    min_confianza: Number(el('c-conf').value),
    fps_objetivo: Number(el('c-fps').value),
    activa: el('c-activa').checked,
    notas: el('c-notas').value.trim(),
  };
  const fallos = [];
  if (!cuerpo.nombre) fallos.push('indica un nombre');
  if (!cuerpo.fuente) fallos.push('indica la fuente (URL RTSP, índice USB o ruta de archivo)');
  if (!(cuerpo.min_confianza >= 0 && cuerpo.min_confianza <= 1)) fallos.push('la confianza mínima va de 0 a 1');
  if (!(cuerpo.fps_objetivo >= 0 && cuerpo.fps_objetivo <= 60)) fallos.push('los FPS objetivo van de 0 a 60');
  if (fallos.length) {
    err.textContent = `Revisa el formulario: ${fallos.join('; ')}.`;
    err.classList.remove('oculta');
    return;
  }
  try {
    if (estado.editando) {
      await api(`/camaras/${estado.editando}`, { method: 'PUT', body: JSON.stringify(cuerpo) });
      aviso('Cámara actualizada', 'ok');
    } else {
      await api('/camaras', { method: 'POST', body: JSON.stringify(cuerpo) });
      aviso('Cámara añadida', 'ok');
    }
    el('modal-camara').close();
    cargarCamaras();
  } catch (e) {
    err.textContent = e.message;
    err.classList.remove('oculta');
  }
}

/* ------------------------------------------------------- hardware detectado */
async function detectarHardware() {
  const tarjeta = el('tarjeta-detectadas');
  const cuerpo = el('tbody-detectadas');
  tarjeta.classList.remove('oculta');
  cuerpo.innerHTML = '<tr><td colspan="6"><div class="esqueleto esq-fila"></div></td></tr>';
  const btn = el('btn-detectar');
  btn.disabled = true;
  try {
    const d = await api(`/camaras-detectadas?escanear_ip=${el('chk-ip').checked}`);
    if (!d.detectadas.length) {
      cuerpo.innerHTML = '<tr><td colspan="6" class="vacio">Ninguna cámara detectada en este host. Revisa permisos del grupo <code>video</code> o añade una fuente RTSP a mano.</td></tr>';
      return;
    }
    cuerpo.innerHTML = d.detectadas.map((c) => `
      <tr>
        <td>${escapar(c.nombre)}</td>
        <td class="num">${escapar(c.device_id || c.indice)}</td>
        <td>${escapar(c.clase === 'ip' ? 'IP' : 'local')}${c.orientacion && c.orientacion !== 'unknown' ? ` · ${escapar(c.orientacion)}` : ''}</td>
        <td class="num">${escapar(c.resolucion || '—')}${c.fps ? ` @ ${c.fps}` : ''}</td>
        <td><span class="insignia ${c.verificada ? 'activa' : 'detenida'}"><span class="punto"></span>${c.verificada ? 'Entrega imagen' : 'Sin verificar'}</span></td>
        <td><button class="btn fantasma" data-usar='${escapar(JSON.stringify({ n: c.nombre, f: c.url || String(c.indice ?? c.device_id), t: c.clase === 'ip' ? 'rtsp' : 'usb', d: c.detalle }))}'>Usar</button></td>
      </tr>`).join('');
  } catch (e) {
    cuerpo.innerHTML = `<tr><td colspan="6" class="vacio">${escapar(e.message)}</td></tr>`;
  } finally {
    btn.disabled = false;
  }
}

/* --------------------------------------------------------------- detecciones */
async function cargarDetecciones() {
  const cuerpo = el('tbody-detecciones');
  cuerpo.innerHTML = Array.from({ length: 6 }, () => '<tr><td colspan="6"><div class="esqueleto esq-fila"></div></td></tr>').join('');
  const f = estado.filtros;
  const q = new URLSearchParams({
    limite: estado.limite,
    desplazamiento: estado.pagina * estado.limite,
    min_confianza: f.min,
  });
  if (f.placa) q.set('placa', f.placa);
  if (f.camara) q.set('camara', f.camara);
  if (f.desde) q.set('desde', new Date(f.desde).toISOString());

  try {
    const d = await api(`/detecciones?${q}`);
    estado.total = d.total;
    pintarDetecciones(d.detecciones);
    const inicio = d.total ? d.desplazamiento + 1 : 0;
    el('sub-tabla').textContent = `${fmt.format(d.total)} coincidencias`;
    el('pag-info').textContent = `${fmt.format(inicio)}–${fmt.format(Math.min(d.total, d.desplazamiento + estado.limite))} de ${fmt.format(d.total)}`;
    el('pag-anterior').disabled = estado.pagina === 0;
    el('pag-siguiente').disabled = (estado.pagina + 1) * estado.limite >= d.total;
  } catch (e) {
    cuerpo.innerHTML = `<tr><td colspan="6" class="vacio">${escapar(e.message)}</td></tr>`;
  }
}

function pintarDetecciones(filas) {
  const cuerpo = el('tbody-detecciones');
  if (!filas.length) {
    cuerpo.innerHTML = '<tr><td colspan="6" class="vacio">Ninguna detección coincide con los filtros aplicados.</td></tr>';
    return;
  }
  cuerpo.innerHTML = filas.map((f) => `
    <tr>
      <td>${f.recorte
        ? `<img class="miniatura" src="${urlImagen(f.recorte)}" alt="Recorte de ${escapar(f.placa)}" loading="lazy" data-recorte="${escapar(f.recorte)}" data-placa="${escapar(f.placa)}" data-momento="${escapar(f.momento)}">`
        : '<span class="sub">—</span>'}</td>
      <td class="placa">${escapar(f.placa)}</td>
      <td>${escapar(nombreCamara(f.camara))}</td>
      <td class="num">${escapar(fechaCorta(f.momento))}</td>
      <td class="num">${f.frame}</td>
      <td>
        <span class="conf ${claseConf(f.confianza)}">
          <span class="barra"><i style="width:${(f.confianza * 100).toFixed(0)}%"></i></span>
          <span>${f.confianza.toFixed(3)}</span>
        </span>
      </td>
    </tr>`).join('');
}

function exportar() {
  const f = estado.filtros;
  const q = new URLSearchParams({ min_confianza: f.min });
  if (f.placa) q.set('placa', f.placa);
  if (f.camara) q.set('camara', f.camara);
  if (f.desde) q.set('desde', new Date(f.desde).toISOString());
  window.open(`${API}/api/detecciones.csv?${q}`, '_blank');
}

/* ------------------------------------------------------------------- sistema */
function pintarSistema(d) {
  const c = d.config;
  const filas = [
    ['Plataforma detectada', d.plataforma],
    ['Modo', c.modo_demo ? 'demostración (datos sintéticos)' : 'producción'],
    ['Directorio de datos', c.directorio_datos],
    ['CSV de detecciones', `${c.csv}${c.csv_existe ? '' : ' (no existe)'}`],
    ['Almacén de cámaras', c.almacen_camaras],
    ['Unidad systemd', c.plantilla_unidad],
    ['Control de servicios', c.control_habilitado ? 'habilitado' : 'deshabilitado'],
    ['Hora del servidor', d.hora_servidor],
  ];
  if (d.almacenamiento) {
    filas.push(['Almacenamiento', `${d.almacenamiento.libre_gb} GB libres de ${d.almacenamiento.total_gb} GB (${d.almacenamiento.usado_pct} % usado)`]);
  }
  el('datos-sistema').innerHTML = filas.map(([k, v]) => `<div><dt>${escapar(k)}</dt><dd>${escapar(v)}</dd></div>`).join('');
}

/* ----------------------------------------------------------------- SSE en vivo */
function conectarEventos() {
  const punto = document.querySelector('#estado-lateral .punto');
  const texto = el('texto-flujo');
  let fuente;
  try {
    fuente = new EventSource(`${API}/api/eventos`);
  } catch {
    texto.textContent = 'Flujo no disponible';
    return;
  }
  fuente.onopen = () => {
    punto.dataset.estado = 'vivo';
    texto.textContent = 'Flujo en vivo';
  };
  fuente.addEventListener('deteccion', (ev) => {
    const f = JSON.parse(ev.data);
    aviso(`Nueva detección: ${f.placa} · ${nombreCamara(f.camara)}`, 'ok');
    if (estado.vista === 'panel') {
      el('tira-recientes').insertAdjacentHTML('afterbegin', tarjetaCaptura(f));
      el('tira-recientes').querySelectorAll('.captura').forEach((n, i) => { if (i > 13) n.remove(); });
      cargarEstado();
    }
    if (estado.vista === 'detecciones' && estado.pagina === 0) cargarDetecciones();
  });
  fuente.onerror = () => {
    punto.dataset.estado = 'error';
    texto.textContent = 'Reconectando al flujo…';
  };
}

/* -------------------------------------------------------------------- eventos */
function iniciarEventosUI() {
  el('btn-recargar').addEventListener('click', () => irA(estado.vista));

  el('rango-horas').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    estado.horas = Number(btn.dataset.horas);
    el('rango-horas').querySelectorAll('button').forEach((b) => b.classList.toggle('activa', b === btn));
    cargarMetricas();
  });

  el('btn-nueva').addEventListener('click', () => abrirModalCamara());
  el('btn-detectar').addEventListener('click', detectarHardware);
  el('chk-ip').addEventListener('change', () => { if (!el('tarjeta-detectadas').classList.contains('oculta')) detectarHardware(); });
  el('form-camara').addEventListener('submit', guardarCamara);
  el('modal-cancelar').addEventListener('click', () => el('modal-camara').close());
  el('env-cerrar').addEventListener('click', () => el('modal-env').close());
  el('env-copiar').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(el('env-contenido').textContent);
      aviso('Configuración copiada', 'ok');
    } catch {
      aviso('El navegador bloqueó el portapapeles', 'mal');
    }
  });
  el('visor-cerrar').addEventListener('click', () => el('modal-imagen').close());

  el('rejilla-camaras').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    if (btn.dataset.accion) accionCamara(btn.dataset.id, btn.dataset.accion);
    if (btn.dataset.editar) abrirModalCamara(estado.camaras.find((c) => c.id === btn.dataset.editar));
    if (btn.dataset.env) mostrarEnv(btn.dataset.env);
    if (btn.dataset.borrar) borrarCamara(btn.dataset.borrar);
  });

  el('tbody-detectadas').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-usar]');
    if (!btn) return;
    const d = JSON.parse(btn.dataset.usar);
    abrirModalCamara({ nombre: d.n, fuente: d.f, tipo: d.t, min_confianza: 0.8, fps_objetivo: 8, activa: true, notas: d.d || '' });
  });

  document.body.addEventListener('click', (ev) => {
    const nodo = ev.target.closest('[data-recorte]');
    if (!nodo || !nodo.dataset.recorte) return;
    el('visor-img').src = urlImagen(nodo.dataset.recorte);
    el('visor-pie').textContent = `${nodo.dataset.placa} · ${fechaCorta(nodo.dataset.momento)}`;
    el('modal-imagen').showModal();
  });

  const filtros = el('filtros');
  let temporizador;
  filtros.addEventListener('input', (ev) => {
    if (ev.target.id === 'f-conf') el('f-conf-val').textContent = Number(ev.target.value).toFixed(2);
    clearTimeout(temporizador);
    temporizador = setTimeout(() => {
      estado.filtros = {
        placa: el('f-placa').value.trim(),
        camara: el('f-camara').value,
        min: Number(el('f-conf').value),
        desde: el('f-desde').value,
      };
      estado.pagina = 0;
      cargarDetecciones();
    }, 260);
  });
  filtros.addEventListener('reset', () => setTimeout(() => {
    estado.filtros = { placa: '', camara: '', min: 0, desde: '' };
    estado.pagina = 0;
    el('f-conf-val').textContent = '0.00';
    cargarDetecciones();
  }, 0));
  el('btn-exportar').addEventListener('click', exportar);
  el('pag-anterior').addEventListener('click', () => { if (estado.pagina > 0) { estado.pagina--; cargarDetecciones(); } });
  el('pag-siguiente').addEventListener('click', () => { estado.pagina++; cargarDetecciones(); });

  setInterval(() => {
    el('reloj').textContent = new Date().toLocaleTimeString('es-ES');
  }, 1000);
  el('reloj').textContent = new Date().toLocaleTimeString('es-ES');
}

/* --------------------------------------------------------------------- inicio */
esqueletoKpis();
iniciarEventosUI();
irA(location.hash.replace('#/', '') || 'panel');
conectarEventos();
setInterval(() => { if (estado.vista === 'panel') cargarEstado(); }, 30000);
