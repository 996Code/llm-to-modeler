import {
  BarChart,
  GaugeChart,
  LineChart,
  PieChart,
  ScatterChart,
} from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import { init, use } from 'echarts/core'
import type { ECharts } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

use([
  BarChart,
  GaugeChart,
  LineChart,
  PieChart,
  ScatterChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
])

export { init }
export type { ECharts }
