import ResultCard from './ResultCard.vue'
import { metrologyPresentation } from './presentation'

export default {
  component: ResultCard,
  canRender: (message: any) => metrologyPresentation(message) !== null,
}
