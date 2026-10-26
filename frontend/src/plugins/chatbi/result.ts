import ResultCard from './ResultCard.vue'
import { biPresentation } from './presentation'

export default {
  component: ResultCard,
  canRender: (message: any) => biPresentation(message) !== null,
}
